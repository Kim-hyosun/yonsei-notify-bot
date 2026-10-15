import os
import re
import smtplib
import ssl
import time
from email.message import EmailMessage
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from google import genai


# ============================================================
# 환경변수
# ============================================================

BOARD_URL = os.environ.get("BOARD_URL")
AI_API_KEY = os.environ.get("AI_API_KEY")
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL")
LAST_POST_FILE = os.environ.get("LAST_POST_FILE", "last_post.txt")


BASE_URL = "https://gse.yonsei.ac.kr"
REQUEST_TIMEOUT = 20


# ============================================================
# 환경변수 검증
# ============================================================

def validate_env():
    required = {
        "BOARD_URL": BOARD_URL,
        "AI_API_KEY": AI_API_KEY,
        "GMAIL_ADDRESS": GMAIL_ADDRESS,
        "GMAIL_APP_PASSWORD": GMAIL_APP_PASSWORD,
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
# Gemini 요약
# ============================================================

def summarize_with_gemini(title, content):
    """
    Gemini를 사용하여 공지사항 핵심 내용을 요약한다.
    """

    client = genai.Client(
        api_key=AI_API_KEY
    )

    prompt = f"""
너는 연세대학교 교육대학원 학생을 위한
공지사항 요약 도우미다.

아래 공지사항을 읽고 학생이 실제로 행동해야 하는
내용을 중심으로 매우 간결하게 요약해라.

[공지 제목]
{title}

[공지 본문]
{content}

다음 형식으로만 작성해라.

핵심:
- 가장 중요한 내용 1~3개

해야 할 일:
- 학생이 해야 하는 일이 있다면 작성
- 없다면 "없음"

일정:
- 신청/제출/시험/행사 등 중요한 날짜
- 없다면 "없음"

주의:
- 기한 엄수, 제출 방법, 대상 등 중요한 주의사항
- 없다면 "없음"

절대로 원문에 없는 내용을 추측하지 마라.
불필요한 인사말이나 설명은 작성하지 마라.
"""

    response = client.models.generate_content(
        model="gemini-3.5-flash-lite",
        contents=prompt
    )

    return response.text.strip()


# ============================================================
# Gmail 이메일 전송
# ============================================================

def send_notice_email(posts_with_summaries):
    message = EmailMessage()
    message["From"] = GMAIL_ADDRESS
    message["To"] = NOTIFY_EMAIL
    message["Subject"] = f"[연세대 교육대학원 새 공지] {len(posts_with_summaries)}건"

    sections = ["연세대학교 교육대학원 새 공지 요약입니다."]
    for post, summary in posts_with_summaries:
        sections.extend([
            "",
            "=" * 60,
            post["title"],
            f"게시글 번호: {post['id']}",
            f"원문: {post['url']}",
            "",
            summary,
        ])
    message.set_content("\n".join(sections))

    context = ssl.create_default_context()
    # GitHub-hosted runners or network routes can drop the implicit-SSL
    # connection on port 465. Try Gmail's STARTTLS endpoint on 587 as well.
    connection_methods = (
        ("STARTTLS", 587),
        ("SSL", 465),
    )
    last_error = None

    for security, port in connection_methods:
        try:
            if security == "STARTTLS":
                with smtplib.SMTP(
                    "smtp.gmail.com",
                    port,
                    timeout=REQUEST_TIMEOUT
                ) as smtp:
                    smtp.ehlo()
                    smtp.starttls(context=context)
                    smtp.ehlo()
                    smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
                    smtp.send_message(message)
            else:
                with smtplib.SMTP_SSL(
                    "smtp.gmail.com",
                    port,
                    context=context,
                    timeout=REQUEST_TIMEOUT
                ) as smtp:
                    smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
                    smtp.send_message(message)

            print(f"이메일 전송 성공: {NOTIFY_EMAIL}")
            return
        except (smtplib.SMTPException, OSError) as error:
            last_error = error
            print(f"Gmail SMTP {security} {port} 연결 실패: {error}")

    raise RuntimeError(
        "Gmail SMTP 전송에 실패했습니다. GMAIL_ADDRESS가 전체 Gmail 주소인지, "
        "GMAIL_APP_PASSWORD가 2단계 인증 후 발급한 앱 비밀번호인지 확인하세요. "
        f"마지막 SMTP 오류: {last_error}"
    ) from last_error


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
    print("연세대 교육대학원 공지사항 크롤러 시작")
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
    summarized_posts = []

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

        # 상세 페이지
        content = get_post_detail(
            post
        )

        print(
            f"본문 길이: {len(content)}"
        )

        # Gemini 요약
        summary = summarize_with_gemini(
            post["title"],
            content
        )

        print()
        print("Gemini 요약:")
        print(summary)

        summarized_posts.append((post, summary))

        # API 호출 간격
        time.sleep(1.5)

    # --------------------------------------------------------
    # 이메일 전송 성공 후 기준 ID 업데이트
    # --------------------------------------------------------

    send_notice_email(summarized_posts)

    save_last_post_id(
        max(post["id"] for post, _ in summarized_posts)
    )

    print()
    print("=" * 60)
    print(
        f"기준 ID 업데이트 완료: "
        f"{max(post['id'] for post, _ in summarized_posts)}"
    )
    print("=" * 60)


if __name__ == "__main__":
    check_latest_notices()
