import os
import re
import sys
import time
import random
import tempfile
import uuid
import requests
import boto3
from pathlib import Path
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

from reels_config import (
    ACCOUNTS, R2_BUCKET, R2_PUBLIC_URL, R2_ENDPOINT, R2_ACCESS_KEY, R2_SECRET_KEY,
    GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN
)

# 릴스 파일명: {제목}.mp4 / {제목}.txt (숫자 프리픽스 불필요, 대본형 직접제작 콘텐츠는 제목만 사용)
_FNAME_RE = re.compile(r'^(.+)\.(mp4|txt)$')

# ── Google Drive 인증 ─────────────────────────────────────────
def get_drive_service():
    creds = Credentials(
        token=None,
        refresh_token=GOOGLE_REFRESH_TOKEN,
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token"
    )
    return build("drive", "v3", credentials=creds)

# ── R2 클라이언트 ─────────────────────────────────────────────
s3 = boto3.client(
    "s3",
    endpoint_url=R2_ENDPOINT,
    aws_access_key_id=R2_ACCESS_KEY,
    aws_secret_access_key=R2_SECRET_KEY,
    region_name="auto",
)

# ── Drive 파일 스캔 ───────────────────────────────────────────
def scan_drive_folder(folder_id):
    """
    Google Drive 폴더 스캔 → 번호별로 mp4/txt 묶기
    반환: { "198": {"mp4": {...}, "txt": {...}}, ... }
    """
    service = get_drive_service()
    results = service.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        fields="files(id, name, mimeType)",
        pageSize=1000,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()

    files = results.get("files", [])
    groups = {}

    for f in files:
        name = f["name"]
        m = _FNAME_RE.match(name)
        if not m:
            continue
        num, ext = m.group(1), m.group(2)
        groups.setdefault(num, {})
        groups[num]["mp4" if ext == "mp4" else "txt"] = {"id": f["id"], "name": name}

    return groups

# ── Drive 파일 다운로드/삭제 ───────────────────────────────────
def download_from_drive(file_id, filename, tmp_dir):
    service = get_drive_service()
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    fpath = os.path.join(tmp_dir, filename)
    with open(fpath, "wb") as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    return fpath

def delete_drive_file(file_id, filename):
    """IG+FB+YT 동시 게시가 모두 끝난 소스 파일을 완전 삭제.
    (예전엔 유튜브 업로드를 나중에 하기 위해 완료보관함으로 이동해뒀지만,
    이제 한 번에 3곳 다 올리므로 소스 보존이 불필요해짐. 로컬에 대본/컷분리/이미지소스 별도 보관 중)"""
    service = get_drive_service()
    service.files().delete(fileId=file_id, supportsAllDrives=True).execute()
    print(f"  Drive 삭제: {filename}")

# ── R2 업로드 ─────────────────────────────────────────────────
def upload_to_r2(file_path, filename):
    key = f"temp/{uuid.uuid4().hex}{Path(filename).suffix}"
    s3.upload_file(file_path, R2_BUCKET, key, ExtraArgs={"ContentType": "video/mp4"})
    return f"{R2_PUBLIC_URL}/{key}"

# ── Instagram API (릴스 전용) ──────────────────────────────────
def create_reels_media(ig_user_id, token, video_url, caption):
    data = {
        "access_token": token,
        "video_url": video_url,
        "media_type": "REELS",
        "caption": caption,
    }
    res = requests.post(f"https://graph.instagram.com/v21.0/{ig_user_id}/media", data=data)
    j = res.json()
    if "id" not in j:
        print(f"  [API 오류] {j}")
    return j.get("id")

def wait_for_video_processing(media_id, token, timeout=300, interval=5):
    if not media_id:
        return False
    elapsed = 0
    while elapsed < timeout:
        res = requests.get(
            f"https://graph.instagram.com/v21.0/{media_id}",
            params={"fields": "status_code", "access_token": token}
        )
        status = res.json().get("status_code")
        print(f"  영상 처리 상태: {status}")
        if status == "FINISHED":
            return True
        if status == "ERROR":
            return False
        time.sleep(interval)
        elapsed += interval
    print(f"  [경고] 영상 처리 대기 시간 초과 ({timeout}초)")
    return False

def publish_media(ig_user_id, token, container_id, retries=4, retry_wait=10):
    """게시 시도. 영상 트랜스코딩이 FINISHED로 떠도 서버 반영이 지연되어
    'Media ID is not available' (code 9007)가 뜰 수 있어 잠시 대기 후 재시도한다."""
    for attempt in range(retries):
        res = requests.post(
            f"https://graph.instagram.com/v21.0/{ig_user_id}/media_publish",
            data={"creation_id": container_id, "access_token": token}
        )
        result = res.json()
        if result.get("id"):
            return result
        err = result.get("error", {})
        if err.get("code") == 9007 and attempt < retries - 1:
            print(f"  [재시도 {attempt + 1}/{retries - 1}] 미디어 준비 안됨, {retry_wait}초 후 재시도")
            time.sleep(retry_wait)
            continue
        return result
    return result

# ── Facebook Reels 게시 ───────────────────────────────────────
def post_facebook_reel(page_id, page_token, video_url, caption, timeout=300, interval=5):
    """Facebook 페이지에 릴스로 동시 게시 (start -> upload(file_url) -> finish 3단계).
    실패해도 IG 게시 결과에는 영향 없도록 호출부에서 예외를 삼킨다."""
    start_res = requests.post(
        f"https://graph.facebook.com/v21.0/{page_id}/video_reels",
        data={"upload_phase": "start", "access_token": page_token},
    )
    start_j = start_res.json()
    video_id = start_j.get("video_id")
    upload_url = start_j.get("upload_url")
    if not video_id or not upload_url:
        print(f"  [FB 릴스 오류] start 단계 실패: {start_j}")
        return False

    up_res = requests.post(
        upload_url,
        headers={"Authorization": f"OAuth {page_token}", "file_url": video_url},
    )
    up_j = up_res.json()
    if not up_j.get("success"):
        print(f"  [FB 릴스 오류] upload 단계 실패: {up_j}")
        return False

    elapsed = 0
    while elapsed < timeout:
        st_res = requests.get(
            f"https://graph.facebook.com/v21.0/{video_id}",
            params={"fields": "status", "access_token": page_token},
        )
        status = st_res.json().get("status", {}).get("video_status")
        print(f"  [FB 릴스] 처리 상태: {status}")
        if status == "ready":
            break
        if status == "error":
            print(f"  [FB 릴스 오류] 처리 실패")
            return False
        time.sleep(interval)
        elapsed += interval

    finish_res = requests.post(
        f"https://graph.facebook.com/v21.0/{page_id}/video_reels",
        data={
            "upload_phase": "finish",
            "video_id": video_id,
            "video_state": "PUBLISHED",
            "description": caption,
            "access_token": page_token,
        },
    )
    finish_j = finish_res.json()
    print(f"  [FB 릴스] 게시 결과: {finish_j}")
    return bool(finish_j.get("success"))

# ── YouTube Shorts 게시 ───────────────────────────────────────
def get_youtube_service(refresh_token):
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
    )
    return build("youtube", "v3", credentials=creds)

def post_youtube_short(refresh_token, video_path, title, description, lang="ja"):
    """유튜브 쇼츠로 업로드(세로/짧은 영상은 자동으로 쇼츠 취급됨).
    실패해도 IG 게시 결과에는 영향 없도록 호출부에서 예외를 삼킨다."""
    service = get_youtube_service(refresh_token)
    body = {
        "snippet": {
            "title": (title or "Shorts")[:100],
            "description": description or "",
            "categoryId": "27",  # 교육
            "defaultLanguage": lang,
            "defaultAudioLanguage": lang,
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,  # 아동용 아님
            "containsSyntheticMedia": True,  # AI로 생성/수정된 콘텐츠임을 고지
        },
    }
    media = MediaFileUpload(video_path, chunksize=-1, resumable=True, mimetype="video/mp4")
    request = service.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        _, response = request.next_chunk()
    print(f"  [YouTube] 게시 결과: video_id={response.get('id')}")
    return response.get("id")

# ── 메인 업로드 함수 ──────────────────────────────────────────
def post_group(lang, num, item):
    config = ACCOUNTS[lang]
    ig_user_id = config["ig_user_id"]
    token = config["access_token"]

    print(f"\n[{lang}] 릴스 '{num}' 업로드 시작")

    with tempfile.TemporaryDirectory() as tmp_dir:
        caption = ""
        if "txt" in item:
            txt_path = download_from_drive(item["txt"]["id"], item["txt"]["name"], tmp_dir)
            caption = open(txt_path, encoding="utf-8").read().strip()

        mp4_item = item["mp4"]
        fpath = download_from_drive(mp4_item["id"], mp4_item["name"], tmp_dir)
        video_url = upload_to_r2(fpath, mp4_item["name"])
        print(f"  URL: {video_url}")

        container_id = create_reels_media(ig_user_id, token, video_url, caption)
        print(f"  컨테이너 ID: {container_id}")

    if not container_id:
        print(f"  [오류] 컨테이너 생성 실패")
        return False

    if not wait_for_video_processing(container_id, token):
        print(f"  [오류] 영상 처리 실패/시간초과")
        return False

    time.sleep(3)
    result = publish_media(ig_user_id, token, container_id)
    print(f"  게시 결과: {result}")

    if result.get("id"):
        print(f"  [{lang}] 릴스 '{num}' 업로드 완료!")

        # Facebook 릴스 동시 게시 (실패해도 IG 게시 결과는 유지)
        fb_page_id = config.get("fb_page_id")
        fb_page_token = config.get("fb_page_token")
        if fb_page_id and fb_page_token:
            print(f"  [Facebook] 릴스 업로드 시작...")
            try:
                post_facebook_reel(fb_page_id, fb_page_token, video_url, caption)
            except Exception as e:
                print(f"  [FB 릴스 오류] 예외 발생: {e}")

        # 유튜브 쇼츠 동시 게시 (실패해도 IG 게시 결과는 유지)
        yt_refresh_token = config.get("youtube_refresh_token")
        if yt_refresh_token:
            print(f"  [YouTube] 쇼츠 업로드 시작...")
            try:
                with tempfile.TemporaryDirectory() as yt_tmp_dir:
                    yt_fpath = download_from_drive(mp4_item["id"], mp4_item["name"], yt_tmp_dir)
                    title = Path(mp4_item["name"]).stem
                    post_youtube_short(yt_refresh_token, yt_fpath, title, caption, lang=lang)
            except Exception as e:
                print(f"  [YouTube 오류] 예외 발생: {e}")

        # IG+FB+YT 게시 시도가 모두 끝난 뒤 소스 파일 완전 삭제
        for key in ("mp4", "txt"):
            if key in item:
                try:
                    delete_drive_file(item[key]["id"], item[key]["name"])
                except Exception as e:
                    print(f"  [Drive 삭제 오류] {e}")

        return True
    else:
        print(f"  [오류] 게시 실패: {result}")
        return False

# ── 단건 업로드 (스케줄러 호출용) ────────────────────────────
def post_one(lang, target=None):
    if lang not in ACCOUNTS:
        print(f"[{lang}] 계정 설정 없음. 현재 {list(ACCOUNTS)}만 가능합니다.")
        sys.exit(1)

    folder_id = ACCOUNTS[lang]["drive_folder_id"]
    groups = scan_drive_folder(folder_id)

    # mp4 없는 항목(캡션 txt만 덩그러니 남은 경우) 제외
    available = {num: item for num, item in groups.items() if "mp4" in item}

    if target:
        if target not in available:
            print(f"[{lang}] target '{target}' 을(를) Drive에서 찾을 수 없음")
            return
        post_group(lang, target, available[target])
        return

    if not available:
        print(f"[{lang}] 업로드 가능한 릴스 없음")
        return

    num = random.choice(list(available.keys()))
    post_group(lang, num, available[num])

if __name__ == "__main__":
    lang = sys.argv[1] if len(sys.argv) > 1 else "ja"
    target = sys.argv[2] if len(sys.argv) > 2 else None
    post_one(lang, target)
