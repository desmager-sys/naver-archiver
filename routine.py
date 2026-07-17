#!/usr/bin/env python3
"""
네이버 아카이버 Cloud Routine — GitHub Actions / 로컬 예약 작업 공용 진입점.
실행 흐름: 크롤링 → Gemini 요약 → Gemini 비판 검토 → Drive 저장 → 이메일 발송
"""
import sys, os, json, re
sys.stdout.reconfigure(encoding="utf-8")

from pathlib import Path
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from cloud_crawl import (
    BLOGS, CAFES, make_session,
    get_blog_recent_urls, fetch_blog_post,
    get_cafe_clubid, get_cafe_menu_ids,
    get_cafe_article_ids, fetch_cafe_post,
    close_selenium_driver,
)
from cloud_drive import (
    get_drive_service, get_or_create_folder,
    save_text_to_drive, safe_filename,
)
from cloud_email import send_email
from analyze import _keys, _call, analyze_post, _daily_count  # Gemini 로직 재사용


# ────────────────────────────────────────────────────────────
# Gemini 배치 요약 — API 요청 수 최소화
# ────────────────────────────────────────────────────────────
_BATCH_SIZE      = 4     # 한 번에 묶을 최대 글 수
_BATCH_CHAR_MAX  = 4000  # 배치 내 총 글자 수 상한 (토큰 절약)
_MAX_PER_SOURCE  = 25    # 소스당 최대 처리 글 수 (캐치업 모드)
_MAX_TOTAL       = 75    # 런당 전체 처리 글 수 상한 (캐치업 모드)

_BACKFILL_MAX_PER_SOURCE = 150   # 백필 회차 소스당 상한 (평소의 6배 — 무제한 아님)
_BACKFILL_MAX_TOTAL      = 300   # 백필 회차 전체 상한
_BACKFILL_MAX_PAGES      = 25    # 백필 회차 블로그 페이지 상한 (평소 5의 5배)


def _summarize_batch(batch: list[dict], keys: list[str]) -> list[str | None]:
    """2~4개 글을 1회 Gemini 호출로 처리. 요청 횟수를 1/N로 절감."""
    if len(batch) == 1:
        return [analyze_post(batch[0]["body"][:5000], keys)]

    items = []
    for i, p in enumerate(batch, 1):
        items.append(
            f"=== 글{i}: [{p['author']}] {p['title'][:40]} ===\n"
            f"{p['body'][:1200]}"
        )

    prompt = (
        f"아래 {len(batch)}개의 투자/경제 글을 각각 간결하게 분석하세요.\n\n"
        + "\n\n".join(items)
        + f"\n\n각 글에 대해 정확히 아래 형식으로 답하세요 (총 {len(batch)}개):\n"
        + "".join(
            f"\n**[글{i}]**\n- 핵심 주장: (1줄)\n- 근거: (1줄)\n- 사고 패턴: (1줄)\n"
            for i in range(1, len(batch) + 1)
        )
    )

    raw = _call(prompt, keys, max_tokens=350 * len(batch))
    if not raw:
        # 배치 실패 → 개별 처리로 폴백
        return [analyze_post(p["body"][:5000], keys) for p in batch]

    # [글N] 마커 기준으로 결과 분리
    results: list[str | None] = [None] * len(batch)
    for i in range(len(batch)):
        marker = f"**[글{i+1}]**"
        nxt    = f"**[글{i+2}]**"
        if marker in raw:
            start = raw.index(marker) + len(marker)
            end   = raw.index(nxt) if nxt in raw else len(raw)
            results[i] = raw[start:end].strip()
    return results


def summarize_all(posts: list[dict], keys: list[str]) -> list[str | None]:
    """
    전체 포스트를 효율적으로 Gemini 요약.
    - 짧은 글(≤1500자): 최대 4개씩 배치 처리 → 요청 횟수 최소화
    - 긴 글(>1500자):   개별 처리 (배치 시 토큰 초과 위험)
    """
    results: list[str | None] = [None] * len(posts)
    batch_idx:   list[int] = []
    batch_chars: int       = 0

    def flush():
        nonlocal batch_chars
        if not batch_idx:
            return
        summaries = _summarize_batch([posts[i] for i in batch_idx], keys)
        for idx, s in zip(batch_idx, summaries):
            results[idx] = s
        batch_idx.clear()
        batch_chars = 0  # noqa: F841  (reassigned in outer scope)

    for i, post in enumerate(posts):
        body_len = len(post.get("body", ""))
        if body_len > 1500:
            flush()
            results[i] = analyze_post(post["body"][:5000], keys)
        else:
            if batch_chars + body_len > _BATCH_CHAR_MAX or len(batch_idx) >= _BATCH_SIZE:
                flush()
            batch_idx.append(i)
            batch_chars += body_len

    flush()
    return results

KST            = timezone(timedelta(hours=9))
SEEN_FILE      = Path(__file__).parent / "seen.json"
LAST_EMAIL_FILE = Path(__file__).parent / "last_email_at.json"
GDRIVE_ROOT    = os.environ.get("GDRIVE_FOLDER_ID", "")

# 새벽 백필 전용 회차(ARCHIVER_BACKFILL=1) — 그날 남은 Gemini 쿼터를 소진할 때까지
# 과거 글을 최대한 크롤링하되, 이메일은 보내지 않는다(조용히 Drive에만 쌓인다).
BACKFILL = os.environ.get("ARCHIVER_BACKFILL", "0") == "1"


# ────────────────────────────────────────────────────────────
# 네이버 로그인 쿠키 만료 감지 — 만료된 뒤에야 크롤이 조용히 저하되는 대신
# 만료 D-5일부터 이메일에 경고 배너를 띄워 미리 재로그인하게 한다.
# ────────────────────────────────────────────────────────────

_COOKIE_WARN_DAYS = 5

def check_cookie_expiry(cookies_json: str) -> str | None:
    if not cookies_json:
        return None
    try:
        cookies = json.loads(cookies_json)
    except Exception:
        return None

    auth_names = {"NID_AUT", "NID_SES", "nid_inf"}
    expiries = [
        c["expiry"] for c in cookies
        if isinstance(c, dict) and c.get("name") in auth_names and c.get("expiry")
    ]
    if not expiries:
        return None

    soonest = datetime.fromtimestamp(min(expiries), KST)
    days_left = (soonest - datetime.now(KST)).total_seconds() / 86400

    if days_left < 0:
        return (
            f"⚠ 네이버 로그인 쿠키가 {abs(days_left):.0f}일 전 만료됨 "
            f"({soonest.strftime('%Y-%m-%d')}) — 카페 크롤이 조용히 누락되고 있을 수 있음. "
            f"setup_login.py 재실행 필요."
        )
    if days_left <= _COOKIE_WARN_DAYS:
        return (
            f"⚠ 네이버 로그인 쿠키 {days_left:.1f}일 후 만료 "
            f"({soonest.strftime('%Y-%m-%d')}) — 만료 전 setup_login.py 재실행 필요."
        )
    return None


# ────────────────────────────────────────────────────────────
# seen.json (방문 URL 추적 — GitHub repo에 커밋)
# ────────────────────────────────────────────────────────────

def load_seen() -> set[str]:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
    return set()


def save_seen(seen: set[str]):
    SEEN_FILE.write_text(
        json.dumps(sorted(seen), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ────────────────────────────────────────────────────────────
# last_email_at.json — 직전에 실제로 이메일을 보낸 시각(이후 생성된 글만
# 다음 이메일에 포함하기 위한 기준점)
# ────────────────────────────────────────────────────────────

def load_last_email_at() -> datetime:
    if LAST_EMAIL_FILE.exists():
        try:
            return datetime.fromisoformat(json.loads(LAST_EMAIL_FILE.read_text(encoding="utf-8"))["at"])
        except Exception:
            pass
    return datetime.fromtimestamp(0, KST)


def save_last_email_at(dt: datetime):
    LAST_EMAIL_FILE.write_text(
        json.dumps({"at": dt.isoformat()}, ensure_ascii=False),
        encoding="utf-8",
    )


# ────────────────────────────────────────────────────────────
# 비판 검토 (Gemini — Anthropic 크레딧에 의존하지 않도록 전환)
# ────────────────────────────────────────────────────────────

def gemini_critique(posts: list[dict], keys: list[str]) -> str:
    if not keys:
        return "(GEMINI_API_KEY 미설정 — 비판 검토 생략)"

    items = []
    for p in posts:
        excerpt = p.get("summary") or p["body"][:600]
        items.append(f"[{p['author']}] {p['title']}\n{excerpt}")

    prompt = f"""오늘 수집된 투자·경제 블로그/카페 글 {len(posts)}건을 비판적으로 검토하세요.

{"=" * 60}
{chr(10).join(items)}
{"=" * 60}

아래 3가지 관점으로 분석하고, 해당 없으면 "해당 없음"으로 표기:

1. **과장된 확신 톤**: "반드시", "확실히", "무조건" 등 과도한 단정 표현
2. **이해관계 프레이밍**: 특정 방향으로 독자를 유도하는 편향 구조
3. **저자 간 모순**: 서로 다른 저자가 상반된 전망을 제시한 경우

마지막에 오늘 핵심 논점 2~3줄 요약.
"""

    result = _call(prompt, keys, max_tokens=1500)
    return result or "(Gemini 비판 검토 실패 — 쿼터 초과 또는 응답 없음)"


# ────────────────────────────────────────────────────────────
# 포맷 헬퍼
# ────────────────────────────────────────────────────────────

def format_drive_md(post: dict) -> str:
    return f"""---
title: {post['title']}
author: {post['author']}
source: {post['source']}
url: {post['url']}
date: {post['date']}
---

{post['body']}

---
## Gemini 요약

{post.get('summary', '(요약 없음)')}
"""


def format_email(posts: list[dict], critique: str, now: datetime, warning: str | None = None) -> str:
    lines = [
        f"네이버 아카이버 자동 수집 — {now.strftime('%Y-%m-%d %H:%M KST')}",
        f"새 글 {len(posts)}건",
        "=" * 60,
        "",
    ]
    if warning:
        lines = [warning, "=" * 60, ""] + lines
    for p in posts:
        lines += [
            f"▶ [{p['author']}] {p['title']}",
            f"   {p['url']}",
            f"   요약: {(p.get('summary') or '(요약 없음)')[:300]}",
            "",
        ]
    lines += ["=" * 60]
    # critique가 실패/생략 마커("(...)" 형태)면 이메일에 원본 오류를 노출하지 않고
    # 섹션 자체를 생략한다 (예: Anthropic 크레딧 부족, 키 미설정 등).
    if critique and not critique.strip().startswith("("):
        lines += ["■ 비판 검토 (Gemini)", "", critique, "", "=" * 60]
    lines += ["(자동 발송 — GitHub Actions)"]
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────
# 메인
# ────────────────────────────────────────────────────────────

def main():
    now = datetime.now(KST)
    print(f"\n{'=' * 60}")
    print(f"네이버 아카이버 실행: {now.strftime('%Y-%m-%d %H:%M KST')}")
    if BACKFILL:
        print("모드: 새벽 백필 전용 (캡 확장, 이메일 미발송)")
    print(f"{'=' * 60}\n")

    # 백필 회차는 평상시보다 훨씬 깊게 과거 글을 찾지만, 무제한은 아니다.
    # (2026-07: 무제한 크롤이 하루 최대 180분씩 GitHub Actions 무료 사용량을
    #  소진시켜 이후 모든 스케줄 실행이 실패한 사고가 있었음 — 유한 캡으로 고정)
    max_per_source = _BACKFILL_MAX_PER_SOURCE if BACKFILL else _MAX_PER_SOURCE
    max_total       = _BACKFILL_MAX_TOTAL      if BACKFILL else _MAX_TOTAL
    blog_want       = _BACKFILL_MAX_TOTAL      if BACKFILL else 0
    blog_max_pages  = _BACKFILL_MAX_PAGES      if BACKFILL else 5

    gemini_keys = _keys()
    cookies_json = os.environ.get("NAVER_COOKIES_JSON", "")
    if not cookies_json:
        # 로컬 실행: GitHub Secrets가 아니라 setup_login.py가 만든 쿠키 파일을 직접 사용
        local_cookie_file = Path(__file__).parent / "naver_cookies.json"
        if local_cookie_file.exists():
            cookies_json = local_cookie_file.read_text(encoding="utf-8")
    cookie_warning = check_cookie_expiry(cookies_json)
    if cookie_warning:
        print(cookie_warning)
    sess = make_session(cookies_json or None)
    seen = load_seen()
    print(f"seen URLs: {len(seen)}개\n")

    new_posts: list[dict] = []

    # ── 블로그 크롤링 ──────────────────────────────────────────
    for blog in BLOGS:
        print(f"[블로그] {blog['name']} ...")
        try:
            urls, date_map = get_blog_recent_urls(
                sess, blog["id"], pages=2, seen=seen, want=blog_want, max_pages=blog_max_pages
            )
            fresh = [u for u in urls if u not in seen]
            print(f"  총 {len(urls)}개 | 새글 {len(fresh)}개")
            for url in fresh[:max_per_source]:
                post = fetch_blog_post(sess, url)
                if not post.get("body"):
                    seen.add(url)
                    continue
                real_dt = date_map.get(url)
                if real_dt:
                    post["date"] = real_dt.strftime("%Y-%m-%d")
                post["published_dt"] = real_dt  # None이면 백필(과거글) — 이메일 대상에서 자동 제외
                post.update({"author": blog["name"], "source": "blog",
                             "blog_id": blog["id"]})
                new_posts.append(post)
                seen.add(url)
        except Exception as e:
            print(f"  오류: {e}")

    # ── 카페 크롤링 ────────────────────────────────────────────
    for cafe in CAFES:
        print(f"\n[카페] {cafe['name']} ...")
        try:
            clubid = get_cafe_clubid(sess, cafe["id"])
            if not clubid:
                print(f"  clubid 조회 실패, 건너뜀"); continue
            print(f"  clubid: {clubid}")

            menu_ids = get_cafe_menu_ids(sess, clubid, cafe["id"])
            print(f"  메뉴 {len(menu_ids)}개")

            for menu_id in menu_ids[:4]:
                try:
                    art_ids = get_cafe_article_ids(sess, clubid, menu_id, pages=1)
                    for aid in art_ids[:max_per_source]:
                        url = f"https://cafe.naver.com/{cafe['id']}/{aid}"
                        if url in seen:
                            continue
                        post = fetch_cafe_post(sess, clubid, aid)
                        if not post.get("body"):
                            seen.add(url)
                            continue
                        # 게시판 목록은 항상 최신순이라, 여기 도달한 글은 seen 기준
                        # 실제로 "처음 발견된" 글 — 정확한 발행시각을 못 얻었으면 크롤 시각으로 대체.
                        post["published_dt"] = post.get("published_dt") or now
                        post.update({"author": cafe["name"], "source": "cafe",
                                     "url": url,
                                     "date": post.get("date") or now.strftime("%Y-%m-%d")})
                        new_posts.append(post)
                        seen.add(url)
                except Exception as e:
                    print(f"  menu {menu_id} 오류: {e}")
        except Exception as e:
            print(f"  오류: {e}")

    close_selenium_driver()

    # 전체 상한 적용 (백필 회차도 _BACKFILL_MAX_TOTAL로 유한하게 제한)
    if len(new_posts) > max_total:
        print(f"  ⚠ 총 {len(new_posts)}개 중 {max_total}개만 처리 (상한 적용)")
        new_posts = new_posts[:max_total]

    print(f"\n새 글 합계: {len(new_posts)}개")
    save_seen(seen)
    print(f"seen.json 갱신 ({len(seen)}개)\n")

    # ── 새 글 없음 ─────────────────────────────────────────────
    if not new_posts:
        if BACKFILL:
            print("백필 대상 없음 (모든 소스가 이미 최신 상태) → 이메일 없이 종료")
            return
        body = f"실행 시각: {now.strftime('%Y-%m-%d %H:%M KST')}\n새로 올라온 글이 없습니다."
        if cookie_warning:
            body = f"{cookie_warning}\n{'=' * 60}\n{body}"
        send_email(
            subject=f"[네이버 아카이버] {now.strftime('%m/%d %H:%M')} — 새 글 없음 ✓",
            body=body,
        )
        print("새 글 없음 → 확인 이메일 발송 완료")
        return

    # ── Gemini 배치 요약 (API 요청 최소화) ────────────────────
    n = len(new_posts)
    est_calls = max(1, -(-n // _BATCH_SIZE))   # ceil division
    print(f"Gemini 요약 중... ({n}건 → 배치 처리, 예상 {est_calls}회 API 호출)")
    summaries = summarize_all(new_posts, gemini_keys)
    for post, s in zip(new_posts, summaries):
        post["summary"] = s or "(요약 실패)"

    used = sum(_daily_count.values())
    print(f"  완료 — 오늘 Gemini 총 호출: {used}회")

    # ── 이메일 대상 선별: 직전 이메일 발송 시각 이후 "생성된" 글만 ──────
    # (백필로 찾은 과거 글은 published_dt가 None이라 여기서 자동 제외된다)
    last_email_at = load_last_email_at()
    email_posts = [p for p in new_posts if p.get("published_dt") and p["published_dt"] > last_email_at]

    # ── 비판 검토 (Gemini, 백필 회차는 생략 — 이메일에 안 쓰이므로 불필요한 호출) ──
    critique = ""
    if not BACKFILL:
        print("\n비판 검토 중 (Gemini)...")
        critique = gemini_critique(email_posts or new_posts, gemini_keys)
        if critique.strip().startswith("("):
            print(f"  건너뜀: {critique}")
        else:
            print("  완료")

    # ── Google Drive 저장 (백필 포함 전체를 저장 — 아카이브는 항상 완전하게) ──
    drive = get_drive_service()
    if drive and GDRIVE_ROOT:
        print("\nDrive 저장 중...")
        for post in new_posts:
            try:
                folder_name = f"{post['author']}_{post.get('blog_id', post['source'])}"
                fid = get_or_create_folder(drive, GDRIVE_ROOT, folder_name)
                fname = f"{post['date']}_{safe_filename(post['title'])}.md"
                save_text_to_drive(drive, format_drive_md(post), fid, fname)
                print(f"  ✓ {fname}")
            except Exception as e:
                print(f"  Drive 저장 오류: {e}")

        # 비판검토 결과 저장 (백필 회차이거나 실패/생략 마커면 저장 안 함)
        if critique and not critique.strip().startswith("("):
            try:
                crit_fid = get_or_create_folder(drive, GDRIVE_ROOT, "비판검토")
                crit_fname = f"{now.strftime('%Y%m%d_%H%M')}_critique.md"
                crit_content = f"# 비판 검토 — {now.strftime('%Y-%m-%d %H:%M KST')}\n\n{critique}"
                save_text_to_drive(drive, crit_content, crit_fid, crit_fname)
                print(f"  ✓ 비판검토/{crit_fname}")
            except Exception as e:
                print(f"  비판검토 Drive 저장 오류: {e}")
    else:
        print("\nDrive 저장 건너뜀 (GDRIVE_REFRESH_TOKEN 또는 GDRIVE_FOLDER_ID 미설정)")

    # ── 이메일 발송 (백필 회차는 발송하지 않음) ──────────────────────
    if BACKFILL:
        print(f"\n(백필 모드) 이메일 발송 생략 — 이번 회차 {len(new_posts)}건은 Drive에만 저장됨")
    elif not email_posts:
        body = (
            f"실행 시각: {now.strftime('%Y-%m-%d %H:%M KST')}\n"
            f"직전 이메일 이후 새로 생성된 글은 없습니다.\n"
            f"(이번 회차에서 백필 등으로 처리된 과거 글 {len(new_posts)}건은 Drive에 저장됨)"
        )
        if cookie_warning:
            body = f"{cookie_warning}\n{'=' * 60}\n{body}"
        send_email(
            subject=f"[네이버 아카이버] {now.strftime('%m/%d %H:%M')} — 새 글 없음 ✓",
            body=body,
        )
        print("직전 메일 이후 신규글 없음 → 확인 이메일 발송 완료")
    else:
        print("\n이메일 발송 중...")
        send_email(
            subject=f"[네이버 아카이버] {now.strftime('%m/%d %H:%M')} — 새 글 {len(email_posts)}건",
            body=format_email(email_posts, critique, now, warning=cookie_warning),
        )
        save_last_email_at(now)
        print(f"  발송 완료 (직전 메일 이후 신규 {len(email_posts)}건 / 이번 회차 전체 크롤 {len(new_posts)}건)")

    print(f"\n{'=' * 60}")
    print(f"완료 — 처리 {len(new_posts)}건")
    print(f"{'=' * 60}\n")


if __name__ == "__main__":
    main()
