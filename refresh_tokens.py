import os
import base64
import requests
from base64 import b64encode
from nacl import encoding, public


def encrypt_secret(public_key: str, secret_value: str) -> str:
    key = public.PublicKey(public_key.encode("utf-8"), encoding.Base64Encoder())
    box = public.SealedBox(key)
    encrypted = box.encrypt(secret_value.encode("utf-8"))
    return b64encode(encrypted).decode("utf-8")


def refresh_ig_token(token):
    resp = requests.get(
        "https://graph.instagram.com/refresh_access_token",
        params={"grant_type": "ig_refresh_token", "access_token": token}
    )
    data = resp.json()
    if "access_token" in data:
        return data["access_token"]
    print(f"  갱신 실패: {data}")
    return token


def update_github_secret(gh_pat, owner, repo, secret_name, secret_value):
    headers = {
        "Authorization": f"token {gh_pat}",
        "Accept": "application/vnd.github.v3+json"
    }
    key_resp = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/actions/secrets/public-key",
        headers=headers
    )
    key_data = key_resp.json()
    encrypted = encrypt_secret(key_data["key"], secret_value)
    requests.put(
        f"https://api.github.com/repos/{owner}/{repo}/actions/secrets/{secret_name}",
        headers=headers,
        json={"encrypted_value": encrypted, "key_id": key_data["key_id"]}
    )


if __name__ == "__main__":
    from reels_config import ACCOUNTS

    with open("reels_config.py", "r", encoding="utf-8") as f:
        config_content = f.read()

    for lang, account in ACCOUNTS.items():
        old_token = account["access_token"]
        print(f"[{lang}] 토큰 갱신 중...")
        new_token = refresh_ig_token(old_token)
        if new_token != old_token:
            config_content = config_content.replace(old_token, new_token)
            print(f"[{lang}] 갱신 완료")
        else:
            print(f"[{lang}] 갱신 실패 (기존 토큰 유지)")

    new_config_b64 = base64.b64encode(config_content.encode("utf-8")).decode("utf-8")

    gh_pat = os.environ["GH_PAT"]
    update_github_secret(gh_pat, "leekyuhwan0-cpu", "reels-auto", "REELS_CONFIG", new_config_b64)
    print("GitHub Secret 업데이트 완료")
