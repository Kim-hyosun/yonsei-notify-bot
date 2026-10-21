import os
import re
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


# ============================================================
# 환경변수
# ============================================================

BOARD_URL = os.environ.get("BOARD_URL")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL")
RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "Yonsei Notice <onboarding@resend.dev>")
LAST_POST_FILE = os.environ.get("LAST_POST_FILE", "last_post.txt")


BASE_URL = "https://gse.yonsei.ac.kr"
REQUEST_TIMEOUT = 20


# ============================================================
# 환경변수 검증
# ============================================================

def validate_env():
    required = {
        "BOARD_URL": BOARD_URL,
        "RESEND_API_KEY": RESEND_API_KEY,
        "NOTIFY_EMAIL": NOTIFY_EMAIL,
    }

    missing = [
        name
        for name, value in required.items()
        if not value
    ]

    if missing:
        raise RuntimeError(
            "필수 환경변수가 없습니다: "
            + ", ".join(missing)
        )


# ============================================================
# HTTP Session
# ============================================================

session = requests.Session()

session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 "
        "(Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/154.0.0.0 Safari/537.36"
    )
})


# ============================================================
# 게시글 번호 추출
# ============================================================

def extract_article_no(href):
    """
    예:
    /gse/board/notice.do?articleNo=481284&mode=view

    → 481284
    """

    match = re.search(
        r"[?&]articleNo=(\d+)",
        href
    )

    if not match:
        return None

    return int(match.group(1))


# ============================================================
# 게시글 제목 정리
# ============================================================

def clean_title(title):
    title = re.sub(r"\s+", " ", title).strip()

    return title


# ============================================================
# 목록 페이지에서 게시글 수집
# ============================================================

def get_posts_from_page(offset=0):
    """
    연세대 교육대학원 공지사항 목록에서
    articleNo를 가진 게시글을 가져온다.
    """

    params = {
        "article.offset": offset,
        "articleLimit": 10,
        "mode": "list",
    }

    response = session.get(
        BOARD_URL,
        params=params,
        timeout=REQUEST_TIMEOUT
    )

    response.raise_for_status()

    soup = BeautifulSoup(
        response.text,
        "html.parser"
    )

    posts = []
    seen_ids = set()

    # 현재 사이트의 실제 상세페이지 링크 구조:
    # notice.do?...articleNo=숫자...&mode=view
    links = soup.select(
        'a[href*="articleNo="]'
    )

    for link in links:

        href = link.get("href", "")

        article_no = extract_article_no(href)

        if article_no is None:
            continue

        if article_no in seen_ids:
            continue

        seen_ids.add(article_no)

        title = clean_title(
            link.get_text(" ", strip=True)
        )

        if not title:
            continue

        full_url = urljoin(
            BASE_URL,
            href
        )

        posts.append({
            "id": article_no,
            "title": title,
            "url": full_url,
        })

    return posts


# ============================================================
# 새 공지 찾기
# ============================================================

def find_new_posts(last_id):
    """
    최신 페이지부터 확인한다.

    매일 실행되므로 일반적으로 첫 페이지에서
    모든 신규 게시글을 찾을 수 있다.

    혹시 며칠 동안 실행되지 않아 신규 게시글이
    10개 이상 쌓인 경우를 대비하여 페이지를 넘긴다.
    """

    new_posts = []

    offset = 0

    # 최대 10페이지까지 확인
    for _ in range(10):

        posts = get_posts_from_page(offset)

        if not posts:
            break

        for post in posts:

            if post["id"] > last_id:
                new_posts.append(post)

        # 현재 페이지에서 가장 작은 ID
        page_min_id = min(
            post["id"]
            for post in posts
        )

        # 이미 과거 게시글 영역으로 내려갔다면 종료
        if page_min_id <= last_id:
            break

        offset += 10

    # 중복 제거
    unique_posts = {}

    for post in new_posts:
        unique_posts[post["id"]] = post

    new_posts = list(
        unique_posts.values()
    )

    # 오래된 공지 → 최신 공지 순
    new_posts.sort(
        key=lambda post: post["id"]
    )

    return new_posts


# ============================================================
# 상세 페이지 본문 추출
# ============================================================

def get_post_detail(post):
    """
    공지 상세 페이지에서 실제 본문을 가져온다.
    """

    response = session.get(
        post["url"],
        timeout=REQUEST_TIMEOUT
    )

    response.raise_for_status()

    soup = BeautifulSoup(
        response.text,
        "html.parser"
    )

    # 불필요한 태그 제거
    for tag in soup.select(
        "script, style, noscript"
    ):
        tag.decompose()

    # 본문 후보
    selectors = [
        ".view_cont",
        ".board_view",
        ".view_content",
        ".boardView",
        ".article-content",
        ".content",
        "#content",
    ]

    content = None

    for selector in selectors:

        element = soup.select_one(selector)

        if element:

            text = element.get_text(
                "\n",
                strip=True
            )

            if len(text) > 30:
                content = text
                break

    # 후보 selector에서 못 찾았을 경우
    # 상세 페이지 전체에서 본문 추정
    if not content:

        text = soup.get_text(
            "\n",
            strip=True
        )

        lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip()
        ]

        content = "\n".join(lines)

    # 너무 긴 페이지 헤더/푸터가 포함되는 경우 대비
    content = re.sub(
        r"\n{3,}",
        "\n\n",
        content
    )

    return content.strip()


# ============================================================
# Resend 이메일 전송
# ============================================================

def send_notice_email(posts):
    text_sections = ["새 공지입니다.\n"]
    html_items = []

    for post in posts:
        text_sections.extend([
            f"- {post['title']}",
            f"  {post['url']}",
            "",
        ])
        html_items.append(
            f"<li style='margin-bottom: 12px;'>"
            f"<strong>{post['title']}</strong><br>"
            f"<a href='{post['url']}' target='_blank'>{post['url']}</a>"
            f"</li>"
        )

    text_content = "\n".join(text_sections)
    html_content = (
        "<h3>새 공지사항이 등록되었습니다.</h3>"
        f"<ul style='line-height: 1.6;'>{''.join(html_items)}</ul>"
    )

    payload = {
        "from": RESEND_FROM_EMAIL,
        "to": [NOTIFY_EMAIL],
        "subject": f"[연세대 헤드위그] 새 공지 {len(posts)}건",
        "text": text_content,
        "html": html_content,
    }

    headers = {
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json",
    }

    errors = []

    for attempt in range(1, 4):
        try:
            response = requests.post(
                "https://api.resend.com/emails",
                headers=headers,
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )

            if response.status_code in (200, 201):
                res_data = response.json()
                print(f"이메일 전송 성공: {NOTIFY_EMAIL} (Resend ID: {res_data.get('id', 'N/A')})")
                return
            else:
                error_msg = f"HTTP {response.status_code}: {response.text}"
                errors.append(f"{attempt}회차: {error_msg}")
                print(f"Resend API 전송 실패 ({attempt}/3): {error_msg}")
        except requests.RequestException as error:
            errors.append(f"{attempt}회차 연결 오류: {error}")
            print(f"Resend API 연결 실패 ({attempt}/3): {error}")

        if attempt < 3:
            time.sleep(attempt * 2)

    raise RuntimeError(
        "Resend API 이메일 전송에 실패했습니다. "
        "RESEND_API_KEY가 올바른지, NOTIFY_EMAIL이 Resend 가입 이메일인지 확인하세요. "
        "상세 오류: " + " | ".join(errors)
    )


# ============================================================
# 마지막 게시글 ID 읽기
# ============================================================

def load_last_post_id():

    if not os.path.exists(
        LAST_POST_FILE
    ):
        return None

    with open(
        LAST_POST_FILE,
        "r",
        encoding="utf-8"
    ) as file:

        value = file.read().strip()

    if value.isdigit():
        return int(value)

    return None


# ============================================================
# 마지막 게시글 ID 저장
# ============================================================

def save_last_post_id(post_id):

    with open(
        LAST_POST_FILE,
        "w",
        encoding="utf-8"
    ) as file:

        file.write(
            str(post_id)
        )


# ============================================================
# Main
# ============================================================

def check_latest_notices():

    validate_env()

    print("=" * 60)
    print("공지사항 크롤러 시작")
    print("=" * 60)

    last_id = load_last_post_id()

    print(
        f"마지막 확인 ID: {last_id}"
    )

    # --------------------------------------------------------
    # 첫 실행
    # --------------------------------------------------------

    if last_id is None:

        posts = get_posts_from_page(
            offset=0
        )

        if not posts:
            raise RuntimeError(
                "공지사항을 찾을 수 없습니다."
            )

        latest_id = max(
            post["id"]
            for post in posts
        )

        save_last_post_id(
            latest_id
        )

        print(
            f"첫 실행이므로 기준 ID만 저장합니다: "
            f"{latest_id}"
        )

        print("기존 공지사항은 이메일로 보내지 않습니다.")

        return

    # --------------------------------------------------------
    # 신규 공지 검색
    # --------------------------------------------------------

    new_posts = find_new_posts(
        last_id
    )

    if not new_posts:

        print(
            "새로운 공지사항이 없습니다."
        )

        return

    print(
        f"새로운 공지 {len(new_posts)}건 발견"
    )

    # --------------------------------------------------------
    new_notices = []

    # --------------------------------------------------------
    # 신규 공지 처리
    # --------------------------------------------------------

    for post in new_posts:

        print()
        print("-" * 60)
        print(
            f"[{post['id']}] "
            f"{post['title']}"
        )
        print(
            post["url"]
        )

        new_notices.append(post)

    # --------------------------------------------------------
    # 이메일 전송 성공 후 기준 ID 업데이트
    # --------------------------------------------------------

    send_notice_email(new_notices)

    save_last_post_id(
        max(post["id"] for post in new_notices)
    )

    print()
    print("=" * 60)
    print(
        f"기준 ID 업데이트 완료: "
        f"{max(post['id'] for post in new_notices)}"
    )
    print("=" * 60)


if __name__ == "__main__":
    check_latest_notices()
