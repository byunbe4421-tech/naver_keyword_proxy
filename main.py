# main.py 맨 위쪽
import os
import time
import hmac
import hashlib
import base64
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

# ==== .env 파일 로딩 설정 ====
BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

# 이 경로에서 .env를 명시적으로 로딩
load_dotenv(dotenv_path=ENV_PATH)

NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET")

if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
    raise RuntimeError(
        f"NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 환경변수가 설정되지 않았습니다. (ENV_PATH={ENV_PATH})"
    )

# 검색광고 키도 같이 읽기
NAVER_AD_API_KEY = os.getenv("NAVER_SEARCH_ACCESS_LICENSE_KEY")
NAVER_AD_SECRET_KEY = os.getenv("NAVER_SEARCH_SECRET_KEY")
NAVER_AD_CUSTOMER_ID = os.getenv("NAVER_SEARCH_CUSTOMER_ID")

# FastAPI 앱 생성
app = FastAPI(
    title="Naver Keyword Proxy",
    version="1.1.0",
    description=(
        "네이버 블로그 검색 API의 total(문서 수)와 "
        "네이버 검색광고 API의 월간 검색수(PC/모바일)를 대신 가져다 주는 프록시 서버"
    ),
)

# ------------------------------------------------------------------
# 공통: 헬스 체크
# ------------------------------------------------------------------
@app.get("/health")
def health():
    """서버 살아있는지 확인용"""
    return {"status": "ok"}


# ------------------------------------------------------------------
# 1단계: 블로그 문서 수 total 조회
# ------------------------------------------------------------------
@app.get("/naver/blog-total")
def blog_total(
    query: str = Query(..., description="네이버 블로그에서 검색할 키워드"),
):
    """
    네이버 블로그 검색 API를 호출해서 total(문서 수)만 깔끔하게 반환하는 프록시.
    """
    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    params = {
        "query": query,
        "display": 1,  # 한 개만 가져와도 total 값은 동일
        "start": 1,
        "sort": "sim",
    }

    try:
        resp = requests.get(
            "https://openapi.naver.com/v1/search/blog.json",
            headers=headers,
            params=params,
            timeout=5,
        )
    except requests.RequestException as e:
        # 네트워크 에러 등
        raise HTTPException(status_code=502, detail=f"네이버 블로그 API 호출 실패: {e}")

    if resp.status_code != 200:
        # 네이버에서 에러 응답이 온 경우
        raise HTTPException(status_code=resp.status_code, detail=resp.text)

    data = resp.json()
    total = data.get("total", 0)

    # GPT가 쓰기 좋게 깔끔한 JSON만 리턴
    try:
        total_int = int(total)
    except (TypeError, ValueError):
        total_int = 0

    return JSONResponse(
        {
            "keyword": query,
            "total": total_int,
        }
    )


# ------------------------------------------------------------------
# 2단계: 네이버 검색광고 월간 검색수 조회
# ------------------------------------------------------------------

# 검색광고 API 기본 설정
SEARCHAD_BASE_URL = "https://api.searchad.naver.com"  # 검색광고 API 도메인
SEARCHAD_URI = "/keywordstool"  # 키워드 도구(월간 검색수 + 연관 키워드)

def make_searchad_signature(timestamp: str, method: str, uri: str, secret_key: str) -> str:
    """
    네이버 검색광고 API에서 요구하는 X-Signature 생성 함수
    message = timestamp.method.uri 형식으로 HMAC-SHA256 후 base64 인코딩
    """
    message = f"{timestamp}.{method}.{uri}"
    h = hmac.new(secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256)
    return base64.b64encode(h.digest()).decode("utf-8")


@app.get("/naver/search-volume")
def search_volume(
    keyword: str = Query(..., description="검색광고에서 조회할 키워드 (hintKeywords)")
):
    """
    네이버 검색광고 API를 사용해서
    - PC 월간 검색수
    - 모바일 월간 검색수
    - 합계(PC+모바일)
    를 가져오는 엔드포인트.
    """
    # 키가 설정 안 되어 있으면 바로 에러
    if not NAVER_AD_API_KEY or not NAVER_AD_SECRET_KEY or not NAVER_AD_CUSTOMER_ID:
        raise HTTPException(
            status_code=500,
            detail="검색광고 API 키(NAVER_SEARCH_*)가 설정되어 있지 않습니다.",
        )

    timestamp = str(round(time.time() * 1000))
    method = "GET"
    uri = SEARCHAD_URI

    signature = make_searchad_signature(timestamp, method, uri, NAVER_AD_SECRET_KEY)

    headers = {
        "X-Timestamp": timestamp,
        "X-API-KEY": NAVER_AD_API_KEY,
        "X-Customer": str(NAVER_AD_CUSTOMER_ID),
        "X-Signature": signature,
    }

    params = {
        "hintKeywords": keyword,
        "showDetail": "1",  # PC/모바일 검색수까지 받기
    }

    try:
        resp = requests.get(
            SEARCHAD_BASE_URL + uri,
            headers=headers,
            params=params,
            timeout=10,
        )
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"네이버 검색광고 API 네트워크 오류: {e}")

    if resp.status_code != 200:
        # 검색광고 API에서 내려주는 에러를 그대로 전달
        raise HTTPException(status_code=resp.status_code, detail=resp.text)

    data = resp.json()
    kw_list = data.get("keywordList", [])

    if not kw_list:
        # 데이터 없을 때 0으로 리턴
        return JSONResponse(
            {
                "keyword": keyword,
                "monthlyPcQcCnt": 0,
                "monthlyMobileQcCnt": 0,
                "monthlyTotalQcCnt": 0,
            }
        )

    # 첫 번째 키워드 정보 사용
    first = kw_list[0]

    # 문자열일 수도 있어서 안전하게 int 변환
    pc = int(first.get("monthlyPcQcCnt", "0") or 0)
    mobile = int(first.get("monthlyMobileQcCnt", "0") or 0)
    total = pc + mobile

    return JSONResponse(
        {
            "keyword": first.get("relKeyword", keyword),
            "monthlyPcQcCnt": pc,
            "monthlyMobileQcCnt": mobile,
            "monthlyTotalQcCnt": total,
        }
    )
