import os
import re
import time
import requests
from bs4 import BeautifulSoup

# 카카오 환경변수 로드
KAKAO_REST_API_KEY = os.environ.get("KAKAO_REST_API_KEY")
KAKAO_REFRESH_TOKEN = os.environ.get("KAKAO_REFRESH_TOKEN")

BOARD_URL = os.environ.get("BOARD_URL")
LAST_POST_FILE = os.environ.get("LAST_POST_FILE")

def get_kakao_access_token():
    """Refresh Token으로 새 Access Token 발급"""
    url = "https://kauth.kakao.com/oauth/token"
    data = {
        "grant_type": "refresh_token",
        "client_id": KAKAO_REST_API_KEY,
        "refresh_token": KAKAO_REFRESH_TOKEN
    }
    response = requests.post(url, data=data)
    res_json = response.json()
    if "access_token" in res_json:
        return res_json["access_token"]
    raise Exception(f"토큰 갱신 실패: {res_json}")

def send_kakao_message(access_token, title, link):
    """제목과 링크만 담아 나에게 카카오톡 메시지 전송"""
    url = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
    headers = {"Authorization": f"Bearer {access_token}"}
    
    payload = {
        "template_object": {
            "object_type": "text",
            "text": f"📢 [연세대 교대원 새 공지]\n\n{title}",
            "link": {
                "web_url": link,
                "mobile_web_url": link
            },
            "button_title": "공지 확인하기"
        }
    }
    res = requests.post(
        url, 
        headers=headers, 
        data={"template_object": requests.compat.json.dumps(payload["template_object"])}
    )
    return res.status_code == 200

def extract_article_no(url):
    """URL에서 articleNo 숫자 값 추출"""
    match = re.search(r"articleNo=(\d+)", url)
    return int(match.group(1)) if match else None

def check_latest_notices():
    # 1. 이전 기록 번호 로드
    last_id = 0
    if os.path.exists(LAST_POST_FILE):
        with open(LAST_POST_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if content.isdigit():
                last_id = int(content)

    # 2. 첫 페이지 목록 가져오기
    headers = {"User-Agent": "Mozilla/5.0"}
    res = requests.get(BOARD_URL, headers=headers)
    res.raise_for_status()
    soup = BeautifulSoup(res.text, "html.parser")

    post_rows = soup.select("table tbody tr, .board_list tbody tr")
    if not post_rows:
        print("게시글 목록을 찾을 수 없습니다.")
        return

    new_posts = []
    max_id_found = last_id

    # 3. 새 글 탐색
    for row in post_rows:
        link_tag = row.find("a")
        if not link_tag:
            continue

        href = link_tag.get("href", "")
        article_no = extract_article_no(href)
        
        if not article_no:
            continue

        if article_no > max_id_found:
            max_id_found = article_no

        if article_no > last_id:
            full_url = href if href.startswith("http") else ("https://gse.yonsei.ac.kr" + href)
            post_title = link_tag.get_text(strip=True)
            new_posts.append({
                "id": article_no,
                "title": post_title,
                "url": full_url
            })

    if not new_posts:
        print("새로운 공지사항이 없습니다.")
        return

    # 4. 오래된 순서대로 정렬 후 카톡 발송 (상세 페이지 접근 불필요)
    new_posts.sort(key=lambda x: x["id"])
    print(f"새 공지 {len(new_posts)}건 발견. 메시지 발송 시작.")

    access_token = get_kakao_access_token()

    for post in new_posts:
        success = send_kakao_message(access_token, post["title"], post["url"])
        if success:
            print(f"발송 완료: [{post['id']}] {post['title']}")
        else:
            print(f"발송 실패: [{post['id']}] {post['title']}")
        
        # API 과호출 방지 지연
        time.sleep(1.5)

    # 5. 기준 ID 갱신
    with open(LAST_POST_FILE, "w", encoding="utf-8") as f:
        f.write(str(max_id_found))
    print(f"기준 ID 업데이트 완료: {max_id_found}")

if __name__ == "__main__":
    check_latest_notices()