"""
Gmail SMTP Mail Service
========================
Gmail SMTP를 통해 HTML 메일을 발송. 인라인 이미지(CID) 첨부 지원.

전제:
    - Gmail 계정 2단계 인증 활성화 후 "앱 비밀번호" 발급 필요
    - .env: SMTP_USER / SMTP_APP_PASSWORD 설정
"""

from __future__ import annotations

import os
import smtplib
import ssl
from dataclasses import dataclass, field
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, make_msgid
from pathlib import Path
from typing import Dict, List, Optional, Union

from tools.mail.config import MailConfig, get_mail_config
from tools.mail.logger import get_logger

log = get_logger("mail")


def _build_ssl_context() -> ssl.SSLContext:
    """사내 SSL 검사(corporate proxy) 환경에서도 동작하는 SSL 컨텍스트.

    우선순위:
        1) SMTP_INSECURE_SKIP_VERIFY=true → 검증 비활성 (escape hatch, 경고 로그)
        2) truststore 사용 가능 → Windows/macOS 시스템 인증서 저장소 사용
           (사내 IT가 회사 루트 CA를 OS 저장소에 설치해 둔 경우 통과)
        3) 기본 certifi 번들
    """
    if os.getenv("SMTP_INSECURE_SKIP_VERIFY", "").lower() in ("true", "1", "yes"):
        log.warning(
            "SMTP_INSECURE_SKIP_VERIFY=true → TLS 검증 비활성 (보안 경고). "
            "사내 정책상 임시 우회 용도로만 사용."
        )
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    try:
        import truststore  # type: ignore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        log.info("truststore 미설치 → certifi 기본 번들 사용. 사내 proxy 환경에서는 'pip install truststore' 권장.")
        return ssl.create_default_context()


@dataclass
class InlineImage:
    """HTML 본문에 <img src="cid:..."> 형태로 임베드되는 인라인 이미지."""
    cid: str                                 # HTML의 cid 식별자
    data: bytes                              # 이미지 바이트
    mime_subtype: str = "png"                # png / jpeg 등


@dataclass
class MailMessage:
    subject: str
    html_body: str
    text_fallback: Optional[str] = None
    inline_images: List[InlineImage] = field(default_factory=list)
    attachments: List[Path] = field(default_factory=list)   # 일반 첨부(현재 미사용)
    # 수신자 override (없으면 .env 값 사용)
    to: Optional[List[str]] = None
    cc: Optional[List[str]] = None


class MailSendError(RuntimeError):
    pass


def send_mail(message: MailMessage, config: Optional[MailConfig] = None) -> Dict[str, Union[bool, str, List[str]]]:
    """
    HTML 메일 발송.

    Returns
    -------
    dict
        success(bool), message(str), to(list[str])
    """
    cfg = config or get_mail_config()

    if not cfg.is_valid:
        miss = ", ".join(cfg.missing_keys())
        raise MailSendError(f"메일 설정 누락: {miss}. .env를 확인하세요.")

    to_list = message.to if message.to is not None else cfg.recipients
    cc_list = message.cc if message.cc is not None else cfg.cc

    if not to_list:
        raise MailSendError("수신자(To)가 비어 있습니다. .env의 MAIL_RECIPIENTS를 설정하세요.")

    # ── MIME 메시지 조립 ─────────────────────────────────────────────
    # multipart/mixed
    #   └─ multipart/related (HTML + 인라인 이미지)
    #         ├─ multipart/alternative (text + html)
    #         └─ inline images...
    msg_root = MIMEMultipart("mixed")
    msg_root["Subject"] = message.subject
    msg_root["From"] = formataddr((cfg.sender_name, cfg.smtp_user))
    msg_root["To"] = ", ".join(to_list)
    if cc_list:
        msg_root["Cc"] = ", ".join(cc_list)

    msg_related = MIMEMultipart("related")
    msg_root.attach(msg_related)

    msg_alt = MIMEMultipart("alternative")
    msg_related.attach(msg_alt)

    # text/plain fallback
    text_body = message.text_fallback or "이 메일은 HTML 형식입니다. HTML을 지원하는 메일 클라이언트로 확인해 주세요."
    msg_alt.attach(MIMEText(text_body, "plain", "utf-8"))
    msg_alt.attach(MIMEText(message.html_body, "html", "utf-8"))

    # 인라인 이미지
    for img in message.inline_images:
        mime_img = MIMEImage(img.data, _subtype=img.mime_subtype)
        # CID는 양쪽 꺽쇠를 포함해야 함
        cid_value = img.cid
        if not cid_value.startswith("<"):
            cid_value = f"<{cid_value}>"
        mime_img.add_header("Content-ID", cid_value)
        mime_img.add_header("Content-Disposition", "inline", filename=f"{img.cid}.{img.mime_subtype}")
        msg_related.attach(mime_img)

    # 일반 첨부(현재 사용 X, 추후 PDF 지원용)
    for att_path in message.attachments:
        try:
            with open(att_path, "rb") as f:
                part = MIMEImage(f.read(), _subtype="octet-stream")
            part.add_header("Content-Disposition", "attachment", filename=att_path.name)
            msg_root.attach(part)
        except Exception as e:
            log.warning(f"첨부 파일 처리 실패 ({att_path}): {e}")

    all_recipients = list(to_list) + list(cc_list)
    log.info(f"메일 발송 시도 → To: {to_list}, Cc: {cc_list}")

    # ── SMTP 전송 ────────────────────────────────────────────────────
    try:
        context = _build_ssl_context()
        if cfg.smtp_port == 465:
            # SSL
            with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, context=context, timeout=30) as smtp:
                smtp.login(cfg.smtp_user, cfg.smtp_app_password)
                smtp.sendmail(cfg.smtp_user, all_recipients, msg_root.as_string())
        else:
            # STARTTLS (587)
            with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as smtp:
                smtp.ehlo()
                if cfg.use_tls:
                    smtp.starttls(context=context)
                    smtp.ehlo()
                smtp.login(cfg.smtp_user, cfg.smtp_app_password)
                smtp.sendmail(cfg.smtp_user, all_recipients, msg_root.as_string())

        log.info(f"메일 발송 성공: '{message.subject}'")
        return {"success": True, "message": "메일 발송 성공", "to": to_list}

    except smtplib.SMTPAuthenticationError as e:
        log.error(f"SMTP 인증 실패: {e}")
        raise MailSendError(
            "Gmail 인증 실패 (앱 비밀번호인지 확인하세요. 2단계 인증 → 앱 비밀번호 발급 후 SMTP_APP_PASSWORD에 입력)"
        ) from e
    except Exception as e:
        log.error(f"메일 발송 실패: {e}")
        raise MailSendError(f"메일 발송 실패: {e}") from e


def make_cid(prefix: str = "img") -> str:
    """고유한 Content-ID 문자열 생성(꺽쇠 없이)."""
    mid = make_msgid(domain="fems.local")
    # <abc@fems.local> → abc.prefix
    inner = mid.strip("<>").split("@")[0]
    return f"{prefix}_{inner}"
