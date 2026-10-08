import json
import os
import re
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from google import genai


# ============================================================
# 환경변수
# ============================================================

KAKAO_REST_API_KEY = os.environ.get("KAKAO_REST_API_KEY")
KAKAO_CLIENT_SECRET = os.environ.get("KAKAO_CLIENT_SECRET")
KAKAO_REFRESH_TOKEN = os.environ.get("KAKAO_REFRESH_TOKEN")

BOARD_URL = os.environ.get("BOARD_URL")
AI_API_KEY = os.environ.get("AI_API_KEY")
LAST_POST_FILE = os.environ.get("LAST_POST_FILE")


BASE_URL = "https://gse.yonsei.ac.kr"
REQUEST_TIMEOUT = 20


# ============================================================
# 환경변수 검증
# ============================================================

def validate_env():
    required = {
        "KAKAO_REST_API_KEY": KAKAO_REST_API_KEY,
        "KAKAO_REFRESH_TOKEN": KAKAO_REFRESH_TOKEN,
        "BOARD_URL": BOARD_URL,
        "AI_API_KEY": AI_API_KEY,
        "LAST_POST_FILE": LAST_POST_FILE,
    }

    # Client Secret은 카카오 앱 설정에서 활성화된 경우 필요
    if KAKAO_CLIENT_SECRET:
        print("Kakao Client Secret 사용")

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
        model="gemini-3.8-flash",
        contents=prompt
    )

    return response.text.strip()


# ============================================================
# Kakao Access Token 갱신
# ============================================================

def get_kakao_access_token():

    url = "https://kauth.kakao.com/oauth/token"

    data = {
        "grant_type": "refresh_token",
        "client_id": KAKAO_REST_API_KEY,
        "refresh_token": KAKAO_REFRESH_TOKEN,
    }

    if KAKAO_CLIENT_SECRET:
        data["client_secret"] = KAKAO_CLIENT_SECRET

    response = requests.post(
        url,
        data=data,
        timeout=REQUEST_TIMEOUT
    )

    try:
        result = response.json()
    except Exception:
        raise RuntimeError(
            "Kakao token API 응답 파싱 실패: "
            + response.text
        )

    if response.status_code != 200:

        raise RuntimeError(
            "Kakao Access Token 갱신 실패: "
            + json.dumps(
                result,
                ensure_ascii=False
            )
        )

    access_token = result.get(
        "access_token"
    )

    if not access_token:
        raise RuntimeError(
            "Kakao 응답에 access_token이 없습니다: "
            + json.dumps(
                result,
                ensure_ascii=False
            )
        )

    return access_token


# ============================================================
# Kakao 메시지 전송
# ============================================================

def send_kakao_message(
    access_token,
    title,
    summary,
    link
):

    url = (
        "https://kapi.kakao.com"
        "/v2/api/talk/memo/default/send"
    )

    headers = {
        "Authorization": (
            f"Bearer {access_token}"
        ),
        "Content-Type": (
            "application/x-www-form-urlencoded"
        ),
    }

    text = (
        "📢 연세대 교육대학원 새 공지\n\n"
        f"📌 {title}\n\n"
        f"{summary}"
    )

    template_object = {
        "object_type": "text",
        "text": text,
        "link": {
            "web_url": link,
            "mobile_web_url": link,
        },
        "button_title": "공지 확인하기",
    }

    response = requests.post(
        url,
        headers=headers,
        data={
            "template_object": json.dumps(
                template_object,
                ensure_ascii=False
            )
        },
        timeout=REQUEST_TIMEOUT
    )

    try:
        result = response.json()
    except Exception:
        result = response.text

    if response.status_code != 200:

        print(
            "Kakao 메시지 전송 실패:",
            response.status_code,
            result
        )

        return False

    print(
        "Kakao 메시지 전송 성공:",
        result
    )

    return True


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

        print(
            "기존 공지사항은 카카오톡으로 보내지 않습니다."
        )

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
    # Kakao Token
    # --------------------------------------------------------

    access_token = (
        get_kakao_access_token()
    )

    max_success_id = last_id

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

        # Kakao 전송
        success = send_kakao_message(
            access_token=access_token,
            title=post["title"],
            summary=summary,
            link=post["url"]
        )

        if not success:

            raise RuntimeError(
                f"카카오톡 전송 실패: "
                f"{post['id']}"
            )

        max_success_id = max(
            max_success_id,
            post["id"]
        )

        # API 호출 간격
        time.sleep(1.5)

    # --------------------------------------------------------
    # 모든 전송 성공 후 기준 ID 업데이트
    # --------------------------------------------------------

    save_last_post_id(
        max_success_id
    )

    print()
    print("=" * 60)
    print(
        f"기준 ID 업데이트 완료: "
        f"{max_success_id}"
    )
    print("=" * 60)


if __name__ == "__main__":
    check_latest_notices()