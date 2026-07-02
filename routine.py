#!/usr/bin/env python3
"""
네이버 아카이버 Cloud Routine — GitHub Actions / 로컬 예약 작업 공용 진입점.
실행 흐름: 크롤링 → Gemini 요약 → Claude 비판 검토 → Drive 저장 → 이메일 발송
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

KST       = timezone(timedelta(hours=9))
SEEN_FILE = Path(__file__).parent / "seen.json"
GDRIVE_ROOT = os.environ.get("GDRIVE_FOLDER_ID", "")


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
# Claude 비판 검토 (Anthropic API 직접 호출)
# ────────────────────────────────────────────────────────────

def claude_critique(posts: list[dict]) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return "(ANTHROPIC_API_KEY 미설정 — 비판 검토 생략)"

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
    except ImportError:
        return "(anthropic 패키지 없음)"

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

    try:
        resp = client.messages.create(
            model="claude-sonnet-4-5-20251022",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text
    except Exception as e:
        return f"(Claude API 오류: {e})"


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


def format_email(posts: list[dict], critique: str, now: datetime) -> str:
    lines = [
        f"네이버 아카이버 자동 수집 — {now.strftime('%Y-%m-%d %H:%M KST')}",
        f"새 글 {len(posts)}건",
        "=" * 60,
        "",
    ]
    for p in posts:
        lines += [
            f"▶ [{p['author']}] {p['title']}",
            f"   {p['url']}",
            f"   요약: {(p.get('summary') or '(요약 없음)')[:300]}",
            "",
        ]
    lines += [
        "=" * 60,
        "■ Claude 비판 검토",
        "",
        critique,
        "",
        "=" * 60,
        "(자동 발송 — GitHub Actions)",
    ]
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────
# 메인
# ────────────────────────────────────────────────────────────

def main():
    now = datetime.now(KST)
    print(f"\n{'=' * 60}")
    print(f"네이버 아카이버 실행: {now.strftime('%Y-%m-%d %H:%M KST')}")
    print(f"{'=' * 60}\n")

    gemini_keys = _keys()
    cookies_json = os.environ.get("NAVER_COOKIES_JSON", "")
    sess = make_session(cookies_json or None)
    seen = load_seen()
    print(f"seen URLs: {len(seen)}개\n")

    new_posts: list[dict] = []

    # ── 블로그 크롤링 ──────────────────────────────────────────
    for blog in BLOGS:
        print(f"[블로그] {blog['name']} ...")
        try:
            urls = get_blog_recent_urls(sess, blog["id"], pages=2)
            fresh = [u for u in urls if u not in seen]
            print(f"  총 {len(urls)}개 | 새글 {len(fresh)}개")
            for url in fresh[:10]:
                post = fetch_blog_post(sess, url)
                if not post.get("body"):
                    seen.add(url)
                    continue
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

            menu_ids = get_cafe_menu_ids(sess, clubid)
            print(f"  메뉴 {len(menu_ids)}개")

            for menu_id in menu_ids[:6]:
                try:
                    art_ids = get_cafe_article_ids(sess, clubid, menu_id, pages=1)
                    for aid in art_ids[:8]:
                        url = f"https://cafe.naver.com/{cafe['id']}/{aid}"
                        if url in seen:
                            continue
                        post = fetch_cafe_post(sess, clubid, aid)
                        if not post.get("body"):
                            seen.add(url)
                            continue
                        post.update({"author": cafe["name"], "source": "cafe",
                                     "url": url,
                                     "date": post.get("date") or now.strftime("%Y-%m-%d")})
                        new_posts.append(post)
                        seen.add(url)
                except Exception as e:
                    print(f"  menu {menu_id} 오류: {e}")
        except Exception as e:
            print(f"  오류: {e}")

    print(f"\n새 글 합계: {len(new_posts)}개")
    save_seen(seen)
    print(f"seen.json 갱신 ({len(seen)}개)\n")

    # ── 새 글 없음 ─────────────────────────────────────────────
    if not new_posts:
        send_email(
            subject=f"[네이버 아카이버] {now.strftime('%m/%d %H:%M')} — 새 글 없음 ✓",
            body=f"실행 시각: {now.strftime('%Y-%m-%d %H:%M KST')}\n새로 올라온 글이 없습니다.",
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

    # ── Claude 비판 검토 ───────────────────────────────────────
    print("\nClaude 비판 검토 중...")
    critique = claude_critique(new_posts)
    print("  완료")

    # ── Google Drive 저장 ──────────────────────────────────────
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

        # 비판검토 결과 저장
        try:
            crit_fid = get_or_create_folder(drive, GDRIVE_ROOT, "비판검토")
            crit_fname = f"{now.strftime('%Y%m%d_%H%M')}_critique.md"
            crit_content = f"# 비판 검토 — {now.strftime('%Y-%m-%d %H:%M KST')}\n\n{critique}"
            save_text_to_drive(drive, crit_content, crit_fid, crit_fname)
            print(f"  ✓ 비판검토/{crit_fname}")
        except Exception as e:
            print(f"  비판검토 Drive 저장 오류: {e}")
    else:
        print("\nDrive 저장 건너뜀 (GDRIVE_SERVICE_ACCOUNT_JSON 또는 GDRIVE_FOLDER_ID 미설정)")

    # ── 이메일 발송 ────────────────────────────────────────────
    print("\n이메일 발송 중...")
    send_email(
        subject=f"[네이버 아카이버] {now.strftime('%m/%d %H:%M')} — 새 글 {len(new_posts)}건",
        body=format_email(new_posts, critique, now),
    )

    print(f"\n{'=' * 60}")
    print(f"완료 — 처리 {len(new_posts)}건")
    print(f"{'=' * 60}\n")


if __name__ == "__main__":
    main()
