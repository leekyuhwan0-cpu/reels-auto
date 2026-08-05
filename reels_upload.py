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
from googleapiclient.http import MediaIoBaseDownload

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

def move_drive_file(file_id, filename, src_folder_id, dest_folder_id):
    """게시 완료된 파일을 삭제하지 않고 완료보관함 폴더로 이동"""
    service = get_drive_service()
    service.files().update(
        fileId=file_id,
        addParents=dest_folder_id,
        removeParents=src_folder_id,
        supportsAllDrives=True,
    ).execute()
    print(f"  Drive 이동(완료보관함): {filename}")

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
        src_folder_id = config["drive_folder_id"]
        dest_folder_id = config["done_folder_id"]
        for key in ("mp4", "txt"):
            if key in item:
                move_drive_file(item[key]["id"], item[key]["name"], src_folder_id, dest_folder_id)
        print(f"  [{lang}] 릴스 '{num}' 업로드 완료!")
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
