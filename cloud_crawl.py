"""
Requests-based Naver crawler — Selenium 없음, 클라우드 호환.
블로그: 모바일 URL (JS 불필요)
카페: Naver 구형 HTML 엔드포인트 + JSON API 폴백
"""
import re, json, time
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))

BLOGS = [
    {"name": "메르",       "id": "ranto28"},
    {"name": "빌딩사령관", "id": "buildingsrg"},
    {"name": "모소밤부",   "id": "bambooinvesting"},
]
CAFES = [
    {"name": "칸트생각", "id": "investmentletter"},
]

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "Referer": "https://www.naver.com/",
}


def make_session(cookies_json: str | None = None) -> requests.Session:
    sess = requests.Session()
    sess.headers.update(_HEADERS)
    if cookies_json:
        try:
            for c in json.loads(cookies_json):
                domain = c.get("domain", ".naver.com")
                sess.cookies.set(c["name"], c["value"], domain=domain)
        except Exception as e:
            print(f"  [세션] 쿠키 로드 오류: {e}")
    return sess


# ────────────────────────────────────────────────────────────
# 블로그
# ────────────────────────────────────────────────────────────

def get_blog_recent_urls(sess: requests.Session, blog_id: str, pages: int = 2) -> list[str]:
    urls: list[str] = []
    for page in range(1, pages + 1):
        try:
            r = sess.get(
                "https://m.blog.naver.com/PostList.naver",
                params={"blogId": blog_id, "categoryNo": "0", "page": page},
                timeout=15,
            )
            soup = BeautifulSoup(r.text, "lxml")
            for a in soup.select("a[href*='blog.naver.com']"):
                m = re.search(r"blog\.naver\.com/(\w+)/(\d{5,})", a.get("href", ""))
                if m:
                    u = f"https://blog.naver.com/{m.group(1)}/{m.group(2)}"
                    if u not in urls:
                        urls.append(u)
            time.sleep(0.8)
        except Exception as e:
            print(f"  [블로그 URL] {blog_id} p{page} 오류: {e}")
    return urls


def fetch_blog_post(sess: requests.Session, url: str) -> dict:
    m = re.search(r"blog\.naver\.com/(\w+)/(\d+)", url)
    if not m:
        return {}
    mob = f"https://m.blog.naver.com/{m.group(1)}/{m.group(2)}"
    try:
        r = sess.get(mob, timeout=15)
        soup = BeautifulSoup(r.text, "lxml")

        title_el = (
            soup.select_one(".se-title-text")
            or soup.select_one('[class*="title_area"]')
            or soup.select_one("h2.se-module-text")
            or soup.select_one("h2")
        )
        body_el = (
            soup.select_one(".se-main-container")
            or soup.select_one("#postViewArea")
            or soup.select_one(".post_ct")
        )
        return {
            "url": url,
            "title": title_el.get_text(strip=True) if title_el else "",
            "body": body_el.get_text(separator="\n", strip=True) if body_el else "",
            "date": datetime.now(KST).strftime("%Y-%m-%d"),
        }
    except Exception as e:
        print(f"  [블로그 본문] {url} 오류: {e}")
        return {}


# ────────────────────────────────────────────────────────────
# 카페
# ────────────────────────────────────────────────────────────

def get_cafe_clubid(sess: requests.Session, cafe_id: str) -> str:
    """카페 메인 페이지에서 clubId(숫자) 추출."""
    try:
        r = sess.get(f"https://cafe.naver.com/{cafe_id}", timeout=15)
        # JSON initial state 또는 URL 파라미터에서 추출
        for pattern in [
            r'"cafeId"\s*:\s*(\d+)',
            r'"clubId"\s*:\s*(\d+)',
            r'clubid=(\d+)',
            r'"id"\s*:\s*(\d+)',
        ]:
            m = re.search(pattern, r.text)
            if m:
                return m.group(1)
    except Exception as e:
        print(f"  [카페 clubid] {cafe_id} 오류: {e}")
    return ""


def get_cafe_menu_ids(sess: requests.Session, clubid: str) -> list[str]:
    """카페 게시판 메뉴 ID 목록 (JSON API)."""
    try:
        api = f"https://apis.naver.com/cafe-web/cafe-web-pc/v1.0/apps/cafes/{clubid}/menus"
        r = sess.get(api, timeout=15)
        data = r.json()
        menus = data.get("message", {}).get("result", {}).get("menus", [])
        return [str(m["menuId"]) for m in menus if m.get("menuType") in ("B", "P", "N")]
    except Exception:
        pass

    # 폴백: 카페 구형 HTML 파싱
    try:
        r = sess.get(
            f"https://cafe.naver.com/CafeList.nhn?clubid={clubid}", timeout=15
        )
        soup = BeautifulSoup(r.text, "lxml")
        ids = []
        for a in soup.select("a[href*='menuid=']"):
            mm = re.search(r"menuid=(\d+)", a.get("href", ""))
            if mm and mm.group(1) not in ids:
                ids.append(mm.group(1))
        return ids[:10]
    except Exception as e:
        print(f"  [카페 메뉴] clubid={clubid} 오류: {e}")
        return []


def get_cafe_article_ids(
    sess: requests.Session, clubid: str, menu_id: str, pages: int = 1
) -> list[str]:
    """게시판별 최신 글 ID 목록 (구형 HTML 엔드포인트)."""
    ids: list[str] = []
    for page in range(1, pages + 1):
        try:
            # 구형 HTML 목록 URL (JS 없이 응답)
            url = (
                f"https://cafe.naver.com/ArticleList.nhn"
                f"?search.clubid={clubid}&search.menuid={menu_id}"
                f"&search.page={page}&search.boardtype=L"
            )
            r = sess.get(url, timeout=15)
            soup = BeautifulSoup(r.text, "lxml")

            for a in soup.select("a[href*='articleid='], a[href*='ArticleRead']"):
                mm = re.search(r"articleid=(\d+)", a.get("href", ""))
                if mm and mm.group(1) not in ids:
                    ids.append(mm.group(1))

            # JSON API 폴백
            if not ids:
                api = (
                    f"https://apis.naver.com/cafe-web/cafe-articleapi/v2"
                    f"/cafes/{clubid}/menus/{menu_id}/articles"
                    f"?page={page}&perPage=15&orderType=date"
                )
                rj = sess.get(api, timeout=15)
                data = rj.json()
                articles = (
                    data.get("message", {}).get("result", {}).get("articleList", [])
                    or data.get("result", {}).get("articleList", [])
                )
                for a in articles:
                    aid = str(a.get("articleId", "") or a.get("id", ""))
                    if aid and aid not in ids:
                        ids.append(aid)

            time.sleep(0.5)
        except Exception as e:
            print(f"  [카페 글목록] menu={menu_id} p{page} 오류: {e}")
    return ids


def fetch_cafe_post(sess: requests.Session, clubid: str, article_id: str) -> dict:
    """단일 카페 글 수집 — 구형 HTML → JSON API 순서로 시도."""
    # 1) 구형 HTML 엔드포인트 (로그인 쿠키 있으면 유료글도 접근 가능)
    try:
        url = f"https://cafe.naver.com/ArticleRead.nhn?clubid={clubid}&articleid={article_id}"
        r = sess.get(url, timeout=15)
        soup = BeautifulSoup(r.text, "lxml")

        title_el = soup.select_one("#td_article h2, .tit-box .tit, h3.title_area")
        body_el = soup.select_one("#tbody, .article-viewer, .SE-main-container")

        if body_el:
            return {
                "title": title_el.get_text(strip=True) if title_el else "",
                "body": body_el.get_text(separator="\n", strip=True),
                "date": datetime.now(KST).strftime("%Y-%m-%d"),
            }
    except Exception:
        pass

    # 2) JSON API 폴백
    try:
        api = (
            f"https://apis.naver.com/cafe-web/cafe-articleapi/v2.1"
            f"/cafes/{clubid}/articles/{article_id}?useCafeId=true&requestFrom=A"
        )
        r = sess.get(api, timeout=15)
        if r.status_code == 200:
            data = r.json()
            art = (
                data.get("message", {}).get("result", {}).get("article", {})
                or data.get("result", {}).get("article", {})
            )
            body_html = art.get("contentHtml", "") or art.get("content", "")
            return {
                "title": art.get("subject", ""),
                "body": BeautifulSoup(body_html, "lxml").get_text(separator="\n", strip=True),
                "date": (art.get("writeDate") or "")[:10],
            }
    except Exception as e:
        print(f"  [카페 본문] article={article_id} 오류: {e}")

    return {}
