#!/usr/bin/env python3
"""斗鱼扫码登录（一次性）：生成二维码 → 用户用斗鱼 APP 扫码 → 轮询确认 → 登录态写入 data/cookie.txt。

用法：python login.py
二维码图片存 data/qr.png（发给用户扫码），data/qr_url.txt 存二维码内容 URL。
"""
import sys
import time

from douyu_auth import DATA, QRLoginSession


def main():
    sess = QRLoginSession()
    try:
        url, expire = sess.start()
    except RuntimeError as e:
        print(e)
        sys.exit(1)

    try:
        png = QRLoginSession.qr_png_bytes(url)
        (DATA / "qr.png").write_bytes(png)
        print(f"二维码已保存: {DATA / 'qr.png'}")
    except ImportError:
        print("未装 qrcode 库，二维码内容 URL:")
        print(url)

    print(f"请用斗鱼 APP 扫码（{expire}s 内有效），等待确认…")
    while True:
        time.sleep(3)
        status, info = sess.poll_once()
        if status == "waiting":
            continue
        if status == "scanned":
            print("已扫码，请在手机上确认…")
            continue
        if status == "expired":
            print(info)
            sys.exit(2)
        if status == "done":
            print(f"登录成功，cookie 已写入 data/cookie.txt（uid={info}）")
            return
        print(info)
        if status == "error" and "换票" in info:
            sys.exit(3)


if __name__ == "__main__":
    main()
