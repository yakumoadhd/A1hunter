"""
A1ハンター - GitHub Actions版
毎回実行時にA1インスタンスの取得を試み、成功したらメール通知して終了。
失敗（空き無し）の場合はワークフロー側でスケジュール再実行。
"""

import os
import sys
import time
import datetime
import subprocess
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ────────────────────────────────────────────────
# 環境変数から設定を読み込む（GitHub Secrets）
# ────────────────────────────────────────────────
USER_OCID        = os.environ["OCI_USER_OCID"]
TENANCY_OCID     = os.environ["OCI_TENANCY_OCID"]
COMPARTMENT_OCID = os.environ["OCI_COMPARTMENT_OCID"]
REGION           = os.environ["OCI_REGION"]
PRIVATE_KEY      = os.environ["OCI_PRIVATE_KEY"]          # 秘密鍵の内容（PEM形式）
FINGERPRINT      = os.environ["OCI_FINGERPRINT"]
NOTIFY_EMAIL     = os.environ["NOTIFY_EMAIL"]
GMAIL_ADDRESS    = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASS   = os.environ["GMAIL_APP_PASSWORD"]

# 1回の実行で何分間リトライするか（GitHub Actionsの無料枠は6時間まで）
HUNT_MINUTES     = int(os.environ.get("HUNT_MINUTES", "50"))
INTERVAL_SECONDS = 120


# ────────────────────────────────────────────────
# oci インストール
# ────────────────────────────────────────────────
def install_oci():
    print("📦 oci インストール中...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "oci", "-q"])
    print("✅ oci インストール完了")


# ────────────────────────────────────────────────
# OCI クライアント初期化
# ────────────────────────────────────────────────
def init_clients():
    import oci

    config = {
        "user":        USER_OCID,
        "tenancy":     TENANCY_OCID,
        "region":      REGION,
        "fingerprint": FINGERPRINT,
        "key_content": PRIVATE_KEY,
    }
    oci.config.validate_config(config)

    network_client = oci.core.VirtualNetworkClient(config)
    compute_client = oci.core.ComputeClient(config)
    identity_client = oci.identity.IdentityClient(config)

    print(f"✅ OCI初期化完了 (リージョン: {REGION})")
    return oci, config, network_client, compute_client, identity_client


# ────────────────────────────────────────────────
# アベイラビリティドメイン取得
# ────────────────────────────────────────────────
def get_ad_name(oci, identity_client):
    ads = identity_client.list_availability_domains(compartment_id=TENANCY_OCID)
    ad_name = ads.data[0].name
    print(f"✅ AD: {ad_name}")
    return ad_name


# ────────────────────────────────────────────────
# サブネット・イメージ取得
# ────────────────────────────────────────────────
def get_subnet_and_image(oci, network_client, compute_client):
    vcns = network_client.list_vcns(compartment_id=COMPARTMENT_OCID).data
    if not vcns:
        raise RuntimeError("VCNが見つかりません")

    subnets = network_client.list_subnets(
        compartment_id=COMPARTMENT_OCID,
        vcn_id=vcns[0].id
    ).data
    subnet_id = subnets[0].id

    images = compute_client.list_images(
        compartment_id=COMPARTMENT_OCID,
        operating_system="Oracle Linux",
        operating_system_version="8",
        shape="VM.Standard.A1.Flex",
        sort_by="TIMECREATED",
        sort_order="DESC"
    ).data
    if not images:
        raise RuntimeError("Oracle Linux 8のイメージが見つかりません")

    image_id = images[0].id
    print(f"✅ サブネット: {subnet_id[:40]}...")
    print(f"✅ イメージ: {images[0].display_name}")
    return subnet_id, image_id


# ────────────────────────────────────────────────
# インスタンス作成試行
# ────────────────────────────────────────────────
def try_create_instance(oci, compute_client, ad_name, subnet_id, image_id):
    try:
        launch_details = oci.core.models.LaunchInstanceDetails(
            availability_domain = ad_name,
            compartment_id      = COMPARTMENT_OCID,
            display_name        = "a1-searxng-server",
            shape               = "VM.Standard.A1.Flex",
            shape_config        = oci.core.models.LaunchInstanceShapeConfigDetails(
                ocpus         = 4,
                memory_in_gbs = 24,
            ),
            source_details      = oci.core.models.InstanceSourceViaImageDetails(
                image_id                = image_id,
                source_type             = "image",
                boot_volume_size_in_gbs = 50,
            ),
            create_vnic_details = oci.core.models.CreateVnicDetails(
                subnet_id        = subnet_id,
                assign_public_ip = True,
            ),
        )
        response = compute_client.launch_instance(launch_details)
        return response.data

    except oci.exceptions.ServiceError as e:
        msg = str(e.message)
        code = str(e.code)
        if "Out of host capacity" in msg or "InternalError" in code:
            return None  # 空き無し（想定内）
        print(f"⚠️ 予期しないAPIエラー: {code} - {msg}")
        return None


# ────────────────────────────────────────────────
# メール通知
# ────────────────────────────────────────────────
def send_gmail(subject, body):
    try:
        msg = MIMEMultipart()
        msg["From"]    = GMAIL_ADDRESS
        msg["To"]      = NOTIFY_EMAIL
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASS)
            server.send_message(msg)
        print("📧 メール送信完了")
    except Exception as e:
        print(f"⚠️ メール送信失敗: {e}")


# ────────────────────────────────────────────────
# メイン
# ────────────────────────────────────────────────
def main():
    print("=" * 50)
    print("🎯 A1ハンター 起動 (GitHub Actions版)")
    print(f"   最大 {HUNT_MINUTES} 分間リトライします")
    print(f"   取れたら {NOTIFY_EMAIL} に通知します")
    print("=" * 50)

    install_oci()
    oci, config, network_client, compute_client, identity_client = init_clients()
    ad_name = get_ad_name(oci, identity_client)
    subnet_id, image_id = get_subnet_and_image(oci, network_client, compute_client)

    deadline = datetime.datetime.now() + datetime.timedelta(minutes=HUNT_MINUTES)
    attempt = 0

    while datetime.datetime.now() < deadline:
        attempt += 1
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{now_str}] 試行 #{attempt} ...", end=" ", flush=True)

        instance = try_create_instance(oci, compute_client, ad_name, subnet_id, image_id)

        if instance:
            print("🎉 成功！！！")
            body = f"""🎉 Oracle A1インスタンスの取得に成功しました！

インスタンス名: {instance.display_name}
インスタンスID: {instance.id}
状態: {instance.lifecycle_state}
作成日時: {now_str}

Oracleコンソールで確認してください：
https://cloud.oracle.com/compute/instances
"""
            send_gmail("🎉【A1ハンター】A1インスタンス取得成功！！", body)
            print("✅ 取得成功！プログラムを終了します。")
            # exit code 0 → ワークフロー側で「成功」と判定してスケジュール停止
            sys.exit(0)

        remaining = (deadline - datetime.datetime.now()).seconds // 60
        print(f"空き無し。次の試行まで {INTERVAL_SECONDS}秒待機... (残り約{remaining}分)")
        time.sleep(INTERVAL_SECONDS)

    print(f"\n⏰ {HUNT_MINUTES}分経過。今回のジョブを終了します（次回スケジュールで再挑戦）。")
    # exit code 0 で正常終了 → GitHub Actionsのスケジュールが次回また実行してくれる
    sys.exit(0)


if __name__ == "__main__":
    main()
