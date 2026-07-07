"""
Daily Energy Intensity Mail - 엔트리 포인트
============================================
.bat 또는 수동 실행:
    python tools/mail/run_daily_mail.py                 # 기본: D-2 (.env 설정)
    python tools/mail/run_daily_mail.py 2026-05-09      # 특정 일자
    python tools/mail/run_daily_mail.py 2026-05-09 --to user1@x.com,user2@x.com
    python tools/mail/run_daily_mail.py --dry-run       # 발송 없이 HTML만 저장
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, date
from pathlib import Path

# tools/mail/file.py → 2단계 위가 프로젝트 루트
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.mail.config import get_mail_config, LOG_DIR
from tools.mail.logger import get_logger
from tools.mail.daily_report_builder import build_daily_report
from tools.mail.mail_service import MailMessage, send_mail, MailSendError


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def main() -> int:
    parser = argparse.ArgumentParser(description="일일 에너지 원단위 메일 자동 송부")
    parser.add_argument("ref_date", nargs="?", default=None,
                        help="기준 일자 YYYY-MM-DD (기본: today - DAILY_REPORT_REFERENCE_OFFSET_DAYS)")
    parser.add_argument("--to", default=None, help="수신자 override (콤마 구분)")
    parser.add_argument("--cc", default=None, help="참조자 override (콤마 구분)")
    parser.add_argument("--dry-run", action="store_true", help="실제 발송 없이 HTML만 logs/automation에 저장")
    args = parser.parse_args()

    log = get_logger("daily_mail")
    log.info("=" * 60)
    log.info("일일 에너지 원단위 메일 자동화 시작")

    # 1) 기준일 결정
    ref = _parse_date(args.ref_date) if args.ref_date else None

    # 2) 리포트 빌드
    try:
        report = build_daily_report(ref_date=ref)
    except Exception as e:
        log.exception(f"리포트 생성 실패: {e}")
        return 2

    if report.record_count == 0:
        log.warning(f"기준일({report.ref_date}) DB 데이터가 없습니다. 그래도 발송을 시도합니다.")

    # 3) 메시지 구성
    msg = MailMessage(
        subject=report.subject,
        html_body=report.html,
        inline_images=report.inline_images,
        to=[x.strip() for x in args.to.split(",") if x.strip()] if args.to else None,
        cc=[x.strip() for x in args.cc.split(",") if x.strip()] if args.cc else None,
    )

    # 4) Dry-run 시 파일로만 저장
    if args.dry_run:
        out = LOG_DIR / f"daily_report_{report.ref_date}_DRYRUN.html"
        out.write_text(report.html, encoding="utf-8")
        log.info(f"[DRY-RUN] HTML 저장: {out}")
        log.info(f"[DRY-RUN] 제목: {report.subject}")
        log.info(f"[DRY-RUN] 인라인 이미지: {len(report.inline_images)}개")
        return 0

    # 5) 발송
    try:
        cfg = get_mail_config()
        if not cfg.is_valid:
            log.error(f"메일 설정 누락: {', '.join(cfg.missing_keys())}. .env 확인 필요.")
            return 3
        send_mail(msg, cfg)
        log.info(f"발송 완료: 기준일 {report.ref_date} / 공장 {report.record_count}개")
        return 0
    except MailSendError as e:
        log.error(f"메일 발송 실패: {e}")
        return 4
    except Exception as e:
        log.exception(f"예상치 못한 오류: {e}")
        return 5


if __name__ == "__main__":
    sys.exit(main())
