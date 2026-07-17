"""
Automation Logger
=================
자동화 .bat 실행마다 파일 + 콘솔에 동시 로깅.

사용:
    from tools.mail.logger import get_logger
    log = get_logger("daily_mail")
    log.info("작업 시작")
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

from tools.mail.config import LOG_DIR


_FORMATTER = logging.Formatter(
    fmt="[%(asctime)s] [%(levelname)-7s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def get_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """
    이름별 로거 반환. 동일 이름으로 재호출되면 기존 핸들러를 재사용.

    Parameters
    ----------
    name : str
        로거 이름. 예: 'daily_mail'
    """
    logger = logging.getLogger(f"automation.{name}")
    logger.setLevel(level)
    logger.propagate = False

    if logger.handlers:
        return logger

    # 파일 핸들러 (날짜별)
    today = datetime.now().strftime("%Y%m%d")
    log_file: Path = LOG_DIR / f"{name}_{today}.log"
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(level)
    fh.setFormatter(_FORMATTER)
    logger.addHandler(fh)

    # 콘솔 핸들러
    sh = logging.StreamHandler(stream=sys.stdout)
    sh.setLevel(level)
    sh.setFormatter(_FORMATTER)
    logger.addHandler(sh)

    return logger
