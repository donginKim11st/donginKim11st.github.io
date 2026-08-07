#!/usr/bin/env python3
"""일일 가격 갱신 스크립트 (Phase 2 자동화).

- scripts/sku-data.json 을 기준 데이터로 각 SKU의 source_url(에누리/다나와)을
  다시 fetch해 최저가·판매처 수를 재파싱한다.
- price/{id}.html 은 재생성하지 않고, 기존 HTML에서 가격 숫자·판매처 수·기준일
  문자열만 컨텍스트 앵커 정규식으로 치환한다 (구조·제목 불변 — A/B 처치 보존).
- JSON-LD lowPrice / offerCount / priceValidUntil 갱신.
- sitemap.xml lastmod 갱신, data/update-log.jsonl append, IndexNow 핑.
- --dry-run: 파일 수정·핑 없이 파싱 결과만 stdout 출력.

H4 계단식 도입(stepped-wedge):
  scripts/wave-assignment.json 의 update_start 이후인 SKU만 갱신한다.
  Wave 1 = 2026-08-19 개시, Wave 2 = 2026-09-02 개시.
  개시 전 SKU는 fetch조차 하지 않고 건너뛴다 (페이지·sitemap·IndexNow 전부 불변).
  --force-all 로 게이트 해제, --as-of YYYY-MM-DD 로 기준일 override(검증용).
"""

import argparse
import calendar
import datetime
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKU_DATA = REPO_ROOT / "scripts" / "sku-data.json"
WAVE_ASSIGN = REPO_ROOT / "scripts" / "wave-assignment.json"
PRICE_DIR = REPO_ROOT / "price"
SITEMAP = REPO_ROOT / "sitemap.xml"
LOG_FILE = REPO_ROOT / "data" / "update-log.jsonl"

SITE_BASE = "https://donginKim11st.github.io"
INDEXNOW_KEY = "e3cb4e38b9644657a7688821ff87c9ad"
INDEXNOW_KEY_LOCATION = f"{SITE_BASE}/{INDEXNOW_KEY}.txt"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
FETCH_SLEEP_SEC = 2
KST = datetime.timezone(datetime.timedelta(hours=9))


def fetch(url: str, timeout: int = 25) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    for enc in ("utf-8", "euc-kr", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def _norm(s: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", s.lower())


ENURI_ITEM_RE = re.compile(
    r'itemprop="name"[^>]*>\s*([^<]{2,150}?)\s*<.*?'
    r'itemprop="lowPrice"[^>]*>\s*([\d,]+)\s*<.*?'
    r'itemprop="offerCount"[^>]*>\s*(\d+)\s*<',
    re.S,
)


def parse_enuri(html: str, sku: dict):
    """에누리 검색 결과에서 대상 모델의 (최저가, 판매처 수)를 찾는다."""
    candidates = []
    for m in ENURI_ITEM_RE.finditer(html):
        name = re.sub(r"\s+", " ", m.group(1)).strip()
        price = int(m.group(2).replace(",", ""))
        count = int(m.group(3))
        if price > 0 and count > 0:
            candidates.append((name, price, count))
    if not candidates:
        return None

    targets = [_norm(sku.get("official_name", "")), _norm(sku.get("name", ""))]
    targets = [t for t in targets if t]

    # 1차: 정규화 후 후보명 ⊆ 공식명 (액세서리/파생모델 오매칭 방지: 역방향 미허용)
    for name, price, count in candidates:
        nc = _norm(name)
        if any(nc and nc in t for t in targets):
            return (name, price, count)

    # 2차: 토큰 겹침 점수 + 기존 판매처 수 근접도로 보정
    old_count = sku.get("seller_count") or 0
    best, best_key = None, None
    for name, price, count in candidates[:10]:
        toks = [_norm(t) for t in name.split() if _norm(t)]
        if not toks:
            continue
        hit = sum(1 for t in toks if any(t in tg for tg in targets))
        score = hit / len(toks)
        if score < 0.75:
            continue
        key = (-score, abs(count - old_count))
        if best_key is None or key < best_key:
            best, best_key = (name, price, count), key
    return best


def parse_danawa(html: str, sku: dict):
    """다나와 상품 상세에서 (최저가, 판매처 수)."""
    price = None
    m = re.search(r'class="lwst_prc"[^>]*>.*?<em[^>]*>([\d,]+)</em>', html, re.S)
    if not m:
        m = re.search(r'"lowPrice"\s*:\s*"?([\d,]+)"?', html)
    if m:
        price = int(m.group(1).replace(",", ""))
    count = None
    m = re.search(r'"offerCount"\s*:\s*"?(\d+)"?', html)
    if not m:
        m = re.search(r"판매처\s*<[^>]*>\s*(\d+)", html)
    if m:
        count = int(m.group(1))
    if price:
        return (sku.get("official_name", ""), price, count or sku.get("seller_count"))
    return None


def collect(sku: dict):
    """source_url fetch + 파싱. 실패 시 None."""
    url = sku.get("source_url", "")
    try:
        html = fetch(url)
    except Exception as e:
        return None, f"fetch error: {e}"
    try:
        if "danawa.com" in url:
            got = parse_danawa(html, sku)
        else:
            got = parse_enuri(html, sku)
    except Exception as e:
        return None, f"parse error: {e}"
    if not got:
        return None, "no matching product in page"
    return got, None


def load_wave_assignment() -> dict:
    """SKU별 갱신 개시일 배정표. 파일이 없으면 빈 dict."""
    if not WAVE_ASSIGN.exists():
        return {}
    data = json.loads(WAVE_ASSIGN.read_text(encoding="utf-8"))
    return data.get("assignment", {})


def wave_active(sid: str, assign: dict, today: datetime.date):
    """(개시 여부, 배정 엔트리). 배정에 없는 SKU는 갱신하지 않는다 (안전 기본값)."""
    ent = assign.get(sid)
    if ent is None:
        return False, None
    return today >= datetime.date.fromisoformat(ent["update_start"]), ent


KOR_DATE_RE = r"\d{4}년 \d{1,2}월 \d{1,2}일"


def rewrite_page(html: str, new_price: int, new_count: int, now: datetime.datetime) -> str:
    """기존 HTML에서 가격·판매처 수·기준일만 컨텍스트 앵커로 치환.

    비고(note) 등 다른 숫자를 건드리지 않도록 위치를 특정한 패턴만 사용한다.
    """
    p = f"{new_price:,}"
    c = str(new_count)
    kdate = f"{now.year}년 {now.month}월 {now.day}일"
    last_day = calendar.monthrange(now.year, now.month)[1]
    valid_until = f"{now.year:04d}-{now.month:02d}-{last_day:02d}"

    subs = [
        # meta description
        (r'(최저가 )[\d,]+(원, 판매처 )\d+(곳 비교\. )' + KOR_DATE_RE + r'( 기준)',
         rf"\g<1>{p}\g<2>{c}\g<3>{kdate}\g<4>"),
        # 본문 도입부
        (r'(최저가는 )[\d,]+(원입니다 \(판매처 )\d+(곳 비교, )' + KOR_DATE_RE + r'( 기준\))',
         rf"\g<1>{p}\g<2>{c}\g<3>{kdate}\g<4>"),
        # 가격 요약 표
        (r'(<tr><th>최저가</th><td>)[\d,]+(원</td></tr>)', rf"\g<1>{p}\g<2>"),
        (r'(<tr><th>판매처 수</th><td>)\d+(곳</td></tr>)', rf"\g<1>{c}\g<2>"),
        (r'(<tr><th>기준일</th><td>)' + KOR_DATE_RE + r'(</td></tr>)', rf"\g<1>{kdate}\g<2>"),
        # FAQ 기준일
        (r'(위 최저가는 )' + KOR_DATE_RE + r'( 기준 수집값)', rf"\g<1>{kdate}\g<2>"),
        # footer 수집 시각 / 갱신일
        (r'\d{4}-\d{2}-\d{2} \d{2}:\d{2} \(KST\) 수집',
         f"{now.strftime('%Y-%m-%d %H:%M')} (KST) 수집"),
        (r'(갱신일: )' + KOR_DATE_RE, rf"\g<1>{kdate}"),
        # JSON-LD
        (r'("lowPrice":\s*)\d+', rf"\g<1>{new_price}"),
        (r'("offerCount":\s*)\d+', rf"\g<1>{new_count}"),
        (r'("priceValidUntil":\s*")\d{4}-\d{2}-\d{2}(")', rf"\g<1>{valid_until}\g<2>"),
    ]
    for pat, rep in subs:
        html = re.sub(pat, rep, html)
    return html


def update_sitemap(updated_ids, now):
    txt = SITEMAP.read_text(encoding="utf-8")
    today = now.strftime("%Y-%m-%d")
    for sid in updated_ids:
        txt = re.sub(
            rf'(<loc>{re.escape(SITE_BASE)}/price/{re.escape(sid)}\.html</loc><lastmod>)[\d-]+(</lastmod>)',
            rf"\g<1>{today}\g<2>", txt)
    SITEMAP.write_text(txt, encoding="utf-8")


def ping_indexnow(updated_ids):
    urls = [f"{SITE_BASE}/price/{sid}.html" for sid in updated_ids]
    payload = json.dumps({
        "host": "donginKim11st.github.io",
        "key": INDEXNOW_KEY,
        "keyLocation": INDEXNOW_KEY_LOCATION,
        "urlList": urls,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.indexnow.org/indexnow", data=payload,
        headers={"Content-Type": "application/json; charset=utf-8",
                 "User-Agent": USER_AGENT},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status
    except Exception as e:
        print(f"[warn] IndexNow ping failed: {e}", file=sys.stderr)
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="파일 수정·핑 없이 파싱 결과만 출력")
    ap.add_argument("--force-all", action="store_true",
                    help="계단식 게이트 무시하고 전 SKU 갱신 (실험 종료 후 전용)")
    ap.add_argument("--as-of", metavar="YYYY-MM-DD",
                    help="게이트 판정 기준일 override (검증용)")
    args = ap.parse_args()

    now = datetime.datetime.now(KST)
    today = (datetime.date.fromisoformat(args.as_of) if args.as_of else now.date())
    skus = json.loads(SKU_DATA.read_text(encoding="utf-8"))
    assign = load_wave_assignment()
    if not assign and not args.force_all:
        print("[abort] scripts/wave-assignment.json 없음 — 계단식 배정 없이는 갱신하지 않는다.",
              file=sys.stderr)
        return 1
    if args.force_all:
        print("[warn] --force-all: 계단식 게이트 해제됨 (H4 처치 오염 주의)", file=sys.stderr)

    results = []      # (sku, matched_name, new_price, new_count) or fail
    changes = []      # 로그용
    waiting = []      # 개시 전 SKU (대조군)
    ok, fail, fetched = 0, 0, 0

    for sku in skus:
        sid = sku["id"]
        page = PRICE_DIR / f"{sid}.html"
        if not page.exists():
            print(f"[skip] {sid}: no price page")
            continue
        active, ent = wave_active(sid, assign, today)
        if not (active or args.force_all):
            w = ent["wave"] if ent else "-"
            st = ent["update_start"] if ent else "-"
            print(f"[wait] {sid}: wave {w}, starts {st}")
            waiting.append({"id": sid, "wave": ent["wave"] if ent else None,
                            "arm": ent["arm"] if ent else None,
                            "update_start": st})
            continue
        if fetched > 0:
            time.sleep(FETCH_SLEEP_SEC)
        fetched += 1
        got, err = collect(sku)
        if got is None:
            fail += 1
            print(f"[fail] {sid}: {err} (기존 값 유지: {sku.get('lowest_price')})")
            changes.append({"id": sid, "status": "failed", "error": err,
                            "wave": ent["wave"] if ent else None,
                            "arm": ent["arm"] if ent else None,
                            "kept_price": sku.get("lowest_price"),
                            "kept_seller_count": sku.get("seller_count")})
            if not args.dry_run:
                sku["status"] = "failed"
            continue
        matched_name, price, count = got
        ok += 1
        old_p, old_c = sku.get("lowest_price"), sku.get("seller_count")
        print(f"[ok]   {sid}: {old_p} -> {price} / sellers {old_c} -> {count}"
              f"  (matched: {matched_name})")
        changes.append({"id": sid, "status": "ok",
                        "wave": ent["wave"] if ent else None,
                        "arm": ent["arm"] if ent else None,
                        "old_price": old_p, "new_price": price,
                        "old_seller_count": old_c, "new_seller_count": count})
        results.append((sku, price, count))
        if not args.dry_run:
            sku["lowest_price"] = price
            sku["seller_count"] = count
            sku["collected_at"] = now.isoformat(timespec="minutes")
            sku["status"] = "ok"

    print(f"\nparsed {ok} ok / {fail} failed / {ok + fail} attempted"
          f" / {len(waiting)} waiting (wave not started)")

    if args.dry_run:
        print("[dry-run] no files modified, no IndexNow ping")
        return 0

    # 갱신 개시된 SKU가 하나도 없으면 완전 no-op으로 종료한다.
    # 로그 1줄이라도 쓰면 커밋이 생기고, GitHub Pages 재배포가 전 페이지의
    # Last-Modified 헤더를 갱신해 H4 신선도 대비를 오염시킨다.
    # 대기 목록은 wave-assignment.json + 날짜로 결정적으로 재구성 가능하므로 정보 손실 없음.
    if not results and not any(c["status"] == "failed" for c in changes):
        print(f"[no-op] 갱신 개시 SKU 0개 ({len(waiting)} 대기) — 파일 미변경, 커밋 없음")
        return 0

    updated_ids = []
    for sku, price, count in results:
        page = PRICE_DIR / f"{sku['id']}.html"
        html = page.read_text(encoding="utf-8")
        new_html = rewrite_page(html, price, count, now)
        if new_html != html:
            page.write_text(new_html, encoding="utf-8")
            updated_ids.append(sku["id"])

    SKU_DATA.write_text(
        json.dumps(skus, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if updated_ids:
        update_sitemap(updated_ids, now)

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "date": now.strftime("%Y-%m-%d"),
            "ts": now.isoformat(timespec="seconds"),
            "gate_date": today.isoformat(),
            "force_all": bool(args.force_all),
            "active_waves": sorted({c["wave"] for c in changes
                                    if c.get("wave") is not None}),
            "updated": [c for c in changes if c["status"] == "ok"],
            "failed": [c for c in changes if c["status"] == "failed"],
            "waiting": waiting,
        }, ensure_ascii=False) + "\n")

    if updated_ids:
        status = ping_indexnow(updated_ids)
        print(f"IndexNow ping: {len(updated_ids)} urls, status={status}")
    print(f"updated pages: {len(updated_ids)}")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
