"""
Requests-based Naver crawler — Selenium 없음, 클라우드 호환.
블로그: 모바일 URL (JS 불필요)
카페: Naver 구형 HTML 엔드포인트 + JSON API 폴백
"""
import re, json, time
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

KST = timezone(timedelta(hours=9))

BLOGS = [
    {"name": "메르",       "id": "ranto28"},
    {"name": "빌딩사령관", "id": "buildingsrg"},
    {"name": "모소밤부",   "id": "bambooinvesting"},
    {"name": "공주필승",   "id": "avandego"},
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



# ────────────────────────────────────────────────────────────
# Selenium 폴백 (카페 전용) — 카페 신규 UI는 클라이언트(JS) 렌더링이라
# requests만으로는 게시글 목록/본문을 가져올 수 없다. selenium/Chrome이
# 설치된 환경(로컬)에서만 활성화되고, 없으면 조용히 건너뛴다(클라우드 호환 유지).
# ────────────────────────────────────────────────────────────
_selenium_driver = None
_selenium_unavailable = False


def _get_selenium_driver():
    global _selenium_driver, _selenium_unavailable
    if _selenium_driver is not None:
        return _selenium_driver
    if _selenium_unavailable:
        return None
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        from webdriver_manager.chrome import ChromeDriverManager

        opts = Options()
        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--window-size=1280,900")
        opts.add_argument(f"user-agent={_HEADERS['User-Agent']}")
        _selenium_driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)
    except Exception as e:
        print(f"  [카페 Selenium] 사용 불가 (건너뜀): {e}")
        _selenium_unavailable = True
        _selenium_driver = None
    return _selenium_driver


def close_selenium_driver():
    global _selenium_driver
    if _selenium_driver:
        try:
            _selenium_driver.quit()
        except Exception:
            pass
    _selenium_driver = None


def _selenium_get_cafe_article_ids(clubid: str, menu_id: str, pages: int = 1) -> list[str]:
    driver = _get_selenium_driver()
    if not driver:
        return []
    ids: list[str] = []
    for page in range(1, pages + 1):
        try:
            driver.get(
                f"https://cafe.naver.com/ArticleList.nhn?search.clubid={clubid}"
                f"&search.menuid={menu_id}&search.boardtype=L&search.page={page}"
            )
            time.sleep(2)
            try:
                driver.switch_to.frame("cafe_main")
            except Exception:
                pass
            soup = BeautifulSoup(driver.page_source, "lxml")
            for a in soup.select("a[href*='articleid='], a[href*='/articles/']"):
                href = a.get("href", "")
                m = re.search(r"articleid=(\d+)|/articles/(\d+)", href)
                if m:
                    aid = m.group(1) or m.group(2)
                    if aid not in ids:
                        ids.append(aid)
            driver.switch_to.default_content()
        except Exception as e:
            print(f"  [카페 Selenium] menu={menu_id} p{page} 오류: {e}")
    return ids


def _selenium_fetch_cafe_post(clubid: str, article_id: str) -> dict:
    driver = _get_selenium_driver()
    if not driver:
        return {}
    try:
        driver.get(f"https://cafe.naver.com/ArticleRead.nhn?clubid={clubid}&articleid={article_id}")
        time.sleep(2)
        try:
            driver.switch_to.frame("cafe_main")
        except Exception:
            pass
        soup = BeautifulSoup(driver.page_source, "lxml")
        title_el = soup.select_one("#td_article h2, .tit-box .tit, h3.title_area, [class*='title_text']")
        body_el = soup.select_one(
            "#tbody, .article-viewer, .SE-main-container, .ArticleContentsArea, .ContentRenderer"
        )
        driver.switch_to.default_content()
        if body_el:
            return {
                "title": title_el.get_text(strip=True) if title_el else "",
                "body": body_el.get_text(separator="\n", strip=True),
                "date": datetime.now(KST).strftime("%Y-%m-%d"),
            }
    except Exception as e:
        print(f"  [카페 Selenium 본문] article={article_id} 오류: {e}")
    return {}


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

def get_blog_recent_urls(
    sess: requests.Session,
    blog_id: str,
    pages: int = 2,
    seen: set | None = None,
    want: int = 25,
    max_pages: int = 200,
) -> tuple[list[str], dict]:
    """
    RSS 피드(최근 50개, 인증 불필요)로 우선 수집.
    RSS 안에서 미방문(seen 기준) 글이 want개 미만이면 — 즉 RSS 캡(50개) 안에서
    당일치 신규글을 다 못 채웠다는 뜻이므로 — 과거 글 백필을 위해
    PostTitleListAsync.naver(총목록 API, totalCount 보유)로 더 깊이 페이지네이션한다.
    이렇게 하면 blog RSS 50개 캡 때문에 놓쳤던 과거 글들이 seen.json에 없는 상태로
    남아있다가, 이후 실행마다 want개씩 점진적으로 백필된다.

    반환값: (urls, date_map). date_map은 RSS의 <pubDate>로 확보한 "진짜" 발행
    시각(KST datetime)만 담는다 — 백필(총목록 API)로 찾은 과거 글은 정확한
    발행 시각을 알 수 없으므로 date_map에 넣지 않는다(= "직전 메일 이후 생성"
    여부를 판단할 때 자동으로 제외되어, 과거 백필 글이 이메일에 섞이지 않는다).
    """
    seen = seen or set()
    urls: list[str] = []
    date_map: dict[str, datetime] = {}

    # 1) RSS 피드 (인증 불필요, JS 불필요) — <item> 블록 단위로 guid + pubDate를 함께 추출
    try:
        r = sess.get(f"https://rss.blog.naver.com/{blog_id}.xml", timeout=15)
        if r.status_code == 200 and "<item>" in r.text:
            for item_m in re.finditer(r"<item>(.*?)</item>", r.text, re.S):
                block = item_m.group(1)
                gm = re.search(r"<guid[^>]*>\s*(https://blog\.naver\.com/\w+/(\d{5,}))\s*</guid>", block)
                if not gm:
                    continue
                u = gm.group(1).strip()
                if u not in urls:
                    urls.append(u)
                pm = re.search(r"<pubDate>([^<]+)</pubDate>", block)
                if pm:
                    try:
                        date_map[u] = parsedate_to_datetime(pm.group(1).strip()).astimezone(KST)
                    except Exception:
                        pass
            # 폴백: URL 패턴 직접 추출 (CDATA 무시, 발행시각은 알 수 없음)
            if not urls:
                for m in re.finditer(rf"https://blog\.naver\.com/{blog_id}/(\d{{5,}})", r.text):
                    u = f"https://blog.naver.com/{blog_id}/{m.group(1)}"
                    if u not in urls:
                        urls.append(u)
    except Exception as e:
        print(f"  [블로그 RSS] {blog_id} 오류: {e}")

    fresh = sum(1 for u in urls if u not in seen)

    # 2) 신규 글이 want개 미만이면 총목록 API로 더 깊이 페이지네이션 (과거글 백필)
    if fresh < want:
        page = (len(urls) // 20) + 1 if urls else 1
        while page <= max_pages:
            try:
                r = sess.get(
                    "https://blog.naver.com/PostTitleListAsync.naver",
                    params={"blogId": blog_id, "categoryNo": "0", "currentPage": page, "countPerPage": "20"},
                    timeout=15,
                )
                ids = re.findall(r'"logNo"\s*:\s*"?(\d{5,})"?', r.text)
                if not ids:
                    break
                added_new = False
                for logno in ids:
                    u = f"https://blog.naver.com/{blog_id}/{logno}"
                    if u not in urls:
                        urls.append(u)
                        added_new = True
                        if u not in seen:
                            fresh += 1
                if not added_new or fresh >= want:
                    break
                time.sleep(0.4)
                page += 1
            except Exception as e:
                print(f"  [블로그 목록] {blog_id} p{page} 오류: {e}")
                break

    if urls:
        return list(dict.fromkeys(urls)), date_map

    # 3) 모바일 HTML 폴백 (위 방법 전부 실패했을 때만, 발행시각은 알 수 없음)
    for page in range(1, pages + 1):
        try:
            r = sess.get(
                "https://m.blog.naver.com/PostList.naver",
                params={"blogId": blog_id, "categoryNo": "0", "page": page},
                timeout=15,
            )
            for m in re.finditer(r"blog\.naver\.com/(\w+)/(\d{5,})", r.text):
                u = f"https://blog.naver.com/{m.group(1)}/{m.group(2)}"
                if u not in urls:
                    urls.append(u)
            time.sleep(0.8)
        except Exception as e:
            print(f"  [블로그 HTML] {blog_id} p{page} 오류: {e}")
    return urls, date_map


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


def get_cafe_menu_ids(sess: requests.Session, clubid: str, cafe_id: str = "") -> list[str]:
    """카페 게시판 메뉴 ID 목록 — 여러 엔드포인트 순차 시도."""
    apis = [
        f"https://apis.naver.com/cafe-web/cafe2/CafeMemberMenuList.json?cafeId={clubid}",
        f"https://cafe.naver.com/CafeMemberList.nhn?clubid={clubid}&search.boardtype=L",
        f"https://apis.naver.com/cafe-web/cafe-web-pc/v1.0/apps/cafes/{clubid}/menus",
    ]
    for api in apis:
        try:
            r = sess.get(api, timeout=15)
            if r.status_code != 200:
                continue
            text = r.text
            # JSON 응답: menuId 또는 menuid 키 추출
            ids = re.findall(r'"(?:menuId|menuid)"\s*:\s*(\d+)', text)
            if ids:
                return list(dict.fromkeys(ids))[:12]
        except Exception:
            pass

    # HTML 폴백: 카페 메인 페이지는 SPA 셸이지만, 좌측 게시판 목록은
    # id="menuLink{menuid}" 형태로 서버사이드 렌더링되어 있어 인증 없이도 파싱 가능.
    # 단, 반드시 카페 URL 슬러그(cafe_id)로 접근해야 한다 — 숫자 clubid로 접근하면
    # "존재하지 않는 카페" 오류 페이지가 뜬다.
    try:
        r = sess.get(f"https://cafe.naver.com/{cafe_id or clubid}", timeout=15)
        ids = [mid for mid in dict.fromkeys(re.findall(r'id="menuLink(\d+)"', r.text)) if mid != "0"]
        if ids:
            return ids[:12]
    except Exception as e:
        print(f"  [카페 메뉴 HTML] clubid={clubid} 오류: {e}")

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

    # 3) 위 방식 전부 실패(신규 카페 UI는 SPA라 requests로 못 읽는 경우) → Selenium 폴백
    if not ids:
        ids = _selenium_get_cafe_article_ids(clubid, menu_id, pages)
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
            text = body_el.get_text(separator="\n", strip=True)
            if text:
                return {
                    "title": title_el.get_text(strip=True) if title_el else "",
                    "body": text,
                    "date": datetime.now(KST).strftime("%Y-%m-%d"),
                    "published_dt": None,  # 정확한 발행시각 불명 (호출측이 크롤 시각으로 대체)
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
            text = BeautifulSoup(body_html, "lxml").get_text(separator="\n", strip=True)
            if text:
                write_date = art.get("writeDate")
                published_dt = None
                if isinstance(write_date, (int, float)):
                    published_dt = datetime.fromtimestamp(write_date / 1000, KST)
                    date_str = published_dt.strftime("%Y-%m-%d")
                elif isinstance(write_date, str) and write_date:
                    date_str = write_date[:10]
                else:
                    date_str = datetime.now(KST).strftime("%Y-%m-%d")
                return {
                    "title": art.get("subject", ""),
                    "body": text,
                    "date": date_str,
                    "published_dt": published_dt,
                }
    except Exception as e:
        print(f"  [카페 본문] article={article_id} 오류: {e}")

    # 3) 위 방식 전부 실패 → Selenium 폴백 (신규 SPA 카페 UI)
    post = _selenium_fetch_cafe_post(clubid, article_id)
    if post.get("body"):
        return post
    return {}
