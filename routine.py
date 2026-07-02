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
from analyze import _keys, _call, analyze_post   # Gemini 로직 재사용

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

    # ── Gemini 요약 ────────────────────────────────────────────
    print("Gemini 요약 중...")
    for i, post in enumerate(new_posts, 1):
        label = f"{post['author']} - {post['title'][:35]}"
        print(f"  [{i}/{len(new_posts)}] {label}...", end="", flush=True)
        summary = analyze_post(post["body"][:5000], gemini_keys)
        post["summary"] = summary or "(요약 실패)"
        print(" ✓")

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
