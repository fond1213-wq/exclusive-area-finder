import re
import time
import requests
import urllib.parse
import pandas as pd
import streamlit as st

# ============================================================
# 1. 기본 설정
# ============================================================

st.set_page_config(
    page_title="전용면적 조회",
    page_icon="🏠",
    layout="wide"
)

# ============================================================
# 2. API KEY 및 기본 설정
# ============================================================

BUILDING_API_BASE = "https://apis.data.go.kr/1613000/BldRgstHubService"
JUSO_API_URL = "https://business.juso.go.kr/addrlink/addrLinkApi.do"

# Secrets 체크
if "BUILDING_API_KEY" not in st.secrets or "JUSO_API_KEY" not in st.secrets:
    st.error("⚠️ Streamlit Secrets 설정이 필요합니다. Settings -> Secrets에서 BUILDING_API_KEY와 JUSO_API_KEY를 입력해주세요.")
    st.stop()

BUILDING_API_KEY = st.secrets["BUILDING_API_KEY"]
JUSO_API_KEY = st.secrets["JUSO_API_KEY"]

# API 키 디코딩 처리 (필요시)
BUILDING_API_KEY_UNQUOTED = urllib.parse.unquote(BUILDING_API_KEY)

# ============================================================
# 3. 문자열 정리
# ============================================================

def normalize_text(value):
    if value is None:
        return ""
    value = str(value).strip()
    value = value.replace(" ", "").replace("　", "")
    return value.upper()

def normalize_dong(value):
    value = normalize_text(value)
    if not value:
        return ""
    value = re.sub(r"^제", "", value)
    value = value.replace("동", "")
    return value

def normalize_ho(value):
    value = normalize_text(value)
    if not value:
        return ""
    value = value.replace("호", "")
    return value

# ============================================================
# 4. 주소에서 동 / 호 추출
# ============================================================

def extract_dong_ho(address):
    text = str(address).strip()
    dong = ""
    ho = ""

    # 101동 1203호
    m = re.search(r"(\d+)\s*동\s*(\d+[A-Za-z가-힣]*)\s*호?\s*$", text, re.IGNORECASE)
    if m:
        dong = m.group(1)
        ho = m.group(2)
        text = text[:m.start()].strip()
        return text, dong, ho

    # 101동1203호
    m = re.search(r"(\d+)\s*동\s*(\d+[A-Za-z가-힣]*)호\s*$", text, re.IGNORECASE)
    if m:
        dong = m.group(1)
        ho = m.group(2)
        text = text[:m.start()].strip()
        return text, dong, ho

    # 1203호
    m = re.search(r"(\d+[A-Za-z가-힣]*)\s*호\s*$", text, re.IGNORECASE)
    if m:
        ho = m.group(1)
        text = text[:m.start()].strip()
        return text, "", ho

    # 마지막 숫자를 호수로 간주 (예: 일성트루엘 715)
    m = re.search(r"\s(\d{2,5})\s*$", text)
    if m:
        ho = m.group(1)
        text = text[:m.start()].strip()

    return text, dong, ho

# ============================================================
# 5. Juso 주소 검색
# ============================================================

def search_juso(address):
    params = {
        "confmKey": JUSO_API_KEY,
        "currentPage": 1,
        "countPerPage": 10,
        "keyword": address,
        "hstryYn": "N",
        "firstSort": "none",
        "addInfoYn": "Y",
        "resultType": "json"
    }

    try:
        response = requests.get(JUSO_API_URL, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        common = data.get("results", {}).get("common", {})
        if common.get("errorCode") != "0":
            return None, f"주소 API 오류: {common.get('errorMessage', '')}"

        juso_list = data.get("results", {}).get("juso", [])
        if not juso_list:
            return None, "도로명주소 검색 결과 없음"

        j = juso_list[0]
        admCd = j.get("admCd", "")
        if len(admCd) < 10:
            return None, "법정동코드 확인 실패"

        sigunguCd = admCd[:5]
        bjdongCd = admCd[5:10]
        jibunAddr = j.get("jibunAddr", "")

        jibun_match = re.search(r"\s(\d+)(?:-(\d+))?(?:\s|$)", jibunAddr)
        if jibun_match:
            bun = jibun_match.group(1)
            ji = jibun_match.group(2) or "0"
        else:
            bun = ""
            ji = "0"

        return {
            "sigunguCd": sigunguCd,
            "bjdongCd": bjdongCd,
            "bun": bun.zfill(4),
            "ji": ji.zfill(4),
            "roadAddr": j.get("roadAddr", ""),
            "jibunAddr": jibunAddr,
            "bdNm": j.get("bdNm", ""),
            "admCd": admCd
        }, "정상"

    except requests.exceptions.Timeout:
        return None, "주소 API 시간 초과"
    except Exception as e:
        return None, f"주소 API 오류: {e}"

# ============================================================
# 6. 건축물대장 API 호출 공통 함수 (JSON 오류 및 API 키 대치 처리)
# ============================================================

def call_building_api(endpoint, params):
    url = f"{BUILDING_API_BASE}/{endpoint}"
    
    # 먼저 Decoding 키 사용
    params["serviceKey"] = BUILDING_API_KEY_UNQUOTED
    params["_type"] = "json"

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        
        # JSON 파싱 시도
        try:
            return response.json()
        except Exception:
            # Encoding 키로 재시도
            params["serviceKey"] = BUILDING_API_KEY
            response = requests.get(url, params=params, timeout=10)
            return response.json()

    except Exception as e:
        raise Exception(f"API 응답 파싱 실패 (서비스 키 확인 필요): {e}")

# ============================================================
# 7. 건축물대장 전유부 조회
# ============================================================

def get_expos_info(sigunguCd, bjdongCd, platGbCd, bun, ji):
    params = {
        "sigunguCd": sigunguCd,
        "bjdongCd": bjdongCd,
        "platGbCd": platGbCd,
        "bun": bun,
        "ji": ji,
        "pageNo": 1,
        "numOfRows": 1000
    }

    data = call_building_api("getBrExposInfo", params)
    body = data.get("response", {}).get("body", {})
    items = body.get("items", {})

    if not items:
        return []

    item_list = items.get("item", [])
    if isinstance(item_list, dict):
        item_list = [item_list]

    return item_list

# ============================================================
# 8. 전유공용면적 조회
# ============================================================

def get_area_info(sigunguCd, bjdongCd, platGbCd, bun, ji, dongNm="", hoNm=""):
    params = {
        "sigunguCd": str(sigunguCd),
        "bjdongCd": str(bjdongCd),
        "platGbCd": str(platGbCd),
        "bun": str(bun).zfill(4),
        "ji": str(ji).zfill(4),
        "pageNo": 1,
        "numOfRows": 1000
    }
    
    # 동/호가 존재하는 경우 파라미터 전달
    if dongNm:
        params["dongNm"] = str(dongNm)
    if hoNm:
        params["hoNm"] = str(hoNm)

    data = call_building_api("getBrExposPubuseAreaInfo", params)
    body = data.get("response", {}).get("body", {})
    items = body.get("items", {})

    if not items:
        return []

    item_list = items.get("item", [])
    if isinstance(item_list, dict):
        item_list = [item_list]

    return item_list

# ============================================================
# 9. 전용면적 매칭
# ============================================================

def find_exclusive_area(expos_items, area_items, target_dong, target_ho):
    target_dong = normalize_dong(target_dong)
    target_ho = normalize_ho(target_ho)

    candidates = []
    for item in expos_items:
        item_dong = normalize_dong(item.get("dongNm", ""))
        item_ho = normalize_ho(item.get("hoNm", ""))

        if target_ho and item_ho != target_ho:
            continue
        if target_dong and item_dong != target_dong:
            continue
        candidates.append(item)

    if not candidates and target_ho:
        for item in expos_items:
            item_ho = normalize_ho(item.get("hoNm", ""))
            if item_ho == target_ho:
                candidates.append(item)

    if not candidates:
        return None, "동/호 매칭 실패", ""

    candidate = candidates[0]
    pk = str(candidate.get("mgmBldrgstPk", ""))
    candidate_dong = normalize_dong(candidate.get("dongNm", ""))
    candidate_ho = normalize_ho(candidate.get("hoNm", ""))

    area_candidates = []
    
    # 1. PK로 우선 비교
    if pk:
        for item in area_items:
            item_pk = str(item.get("mgmBldrgstPk", ""))
            gb_cd = str(item.get("exposPubuseGbCd", "")).strip()
            gb_nm = str(item.get("exposPubuseGbCdNm", "")).strip()

            if (gb_cd == "1" or gb_nm == "전유") and item_pk == pk:
                area_candidates.append(item)

    # 2. PK 비교 실패 시 동/호수로 비교
    if not area_candidates:
        for item in area_items:
            item_dong = normalize_dong(item.get("dongNm", ""))
            item_ho = normalize_ho(item.get("hoNm", ""))
            gb_cd = str(item.get("exposPubuseGbCd", "")).strip()
            gb_nm = str(item.get("exposPubuseGbCdNm", "")).strip()

            if not (gb_cd == "1" or gb_nm == "전유"):
                continue

            if candidate_dong and candidate_ho:
                if item_dong == candidate_dong and item_ho == candidate_ho:
                    area_candidates.append(item)
            elif candidate_ho:
                if item_ho == candidate_ho:
                    area_candidates.append(item)

    if not area_candidates:
        return None, "전유면적 자료 없음", pk

    total_area = 0.0
    for item in area_candidates:
        try:
            area = float(str(item.get("area", "0")).replace(",", ""))
            total_area += area
        except ValueError:
            pass

    if total_area <= 0:
        return None, "전용면적 값 없음", pk

    return round(total_area, 2), "정상", pk

# ============================================================
# 10. 단일 주소 처리
# ============================================================

def process_address(address, progress=None):
    address = str(address).strip()
    if not address:
        return {"주소": "", "전용면적": "", "상태": "주소 없음"}

    try:
        base_address, target_dong, target_ho = extract_dong_ho(address)

        if progress:
            progress.write(f"① 주소정보 검색 중: {base_address}")

        m = re.search(r"^(.+\s+\d+(?:-\d+)?)\s+", base_address)
        juso_search_address = m.group(1) if m else base_address

        # Juso API 조회
        juso, msg = search_juso(juso_search_address)
        if juso is None:
            return {"주소": address, "전용면적": "", "상태": msg}

        if progress:
            progress.write(f"② 주소 확인 완료: {juso['roadAddr']}")
            progress.write("③ 건축물대장 전유부 조회 중...")

        platGbCd = "0"
        expos_items = get_expos_info(
            juso["sigunguCd"], juso["bjdongCd"], platGbCd, juso["bun"], juso["ji"]
        )

        # 산번지 재시도
        if not expos_items:
            platGbCd = "1"
            expos_items = get_expos_info(
                juso["sigunguCd"], juso["bjdongCd"], platGbCd, juso["bun"], juso["ji"]
            )

        if not expos_items:
            return {"주소": address, "전용면적": "", "상태": "건축물대장 전유부 없음"}

        if progress:
            progress.write("④ 전유부 확인 완료 -> ⑤ 전유공용면적 조회 중...")

        # 조건에 상관없이 넓게 검색하여 가져오기
        area_items = get_area_info(
            juso["sigunguCd"],
            juso["bjdongCd"],
            platGbCd,
            juso["bun"],
            juso["ji"]
        )

        if not area_items:
            return {"주소": address, "전용면적": "", "상태": "전유공용면적 조회 실패"}

        area, status, pk = find_exclusive_area(expos_items, area_items, target_dong, target_ho)

        if area is None:
            return {"주소": address, "전용면적": "", "상태": status}

        return {"주소": address, "전용면적": f"{area:.2f}㎡", "상태": "조회 성공"}

    except Exception as e:
        return {"주소": address, "전용면적": "", "상태": str(e)}

# ============================================================
# 11. UI 화면
# ============================================================

st.title("🏠 건축물 전용면적 조회")
st.write("주소를 입력하면 건축물대장 기준 전용면적을 조회합니다.")

addresses = []
for i in range(10):
    address = st.text_input(
        f"주소 {i + 1}",
        key=f"address_{i}",
        placeholder="도로명주소 + 건물명 + 동/호수"
    )
    addresses.append(address.strip())

if st.button("🔎 전용면적 조회", type="primary", use_container_width=True):
    targets = [x for x in addresses if x]

    if not targets:
        st.warning("주소를 하나 이상 입력해주세요.")
        st.stop()

    st.markdown("---")
    st.subheader(f"조회 중... (총 {len(targets)}개 주소)")

    results = []
    for idx, address in enumerate(targets, start=1):
        st.markdown(f"### {idx}. {address}")
        progress_box = st.empty()

        result = process_address(address, progress_box)
        results.append(result)

        if result["전용면적"]:
            st.success(f"전용면적: {result['전용면적']}")
        else:
            st.error(f"조회 실패: {result['상태']}")

        time.sleep(0.2)

    st.markdown("---")
    st.subheader("📋 조회 결과")
    st.dataframe(results, use_container_width=True, hide_index=True)

    result_df = pd.DataFrame(results)
    csv_data = result_df.to_csv(index=False, encoding="utf-8-sig")

    st.download_button(
        "📥 결과 CSV 다운로드",
        data=csv_data,
        file_name="전용면적_조회결과.csv",
        mime="text/csv",
        use_container_width=True
    )
