"""
執行方式: python scripts/gen_password_hash.py
產生 bcrypt hash 後貼到 config/settings.py 的 AUTH_PASSWORD_HASH
"""
import getpass
import bcrypt

password = getpass.getpass("輸入登入密碼: ")
confirm = getpass.getpass("再輸入一次: ")

if password != confirm:
    print("兩次輸入不一致，請重試。")
    raise SystemExit(1)

hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
print("\n請將以下內容貼到 config/settings.py 的 AUTH_PASSWORD_HASH：\n")
print(f'AUTH_PASSWORD_HASH = "{hashed}"')
