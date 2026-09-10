import re
import time
import requests
import streamlit as st
from urllib.parse import unquote


# ============================================================
# 기본 설정
# ============================================================

st.set_page_config(
    page_title="전용면적 조회",
    page_icon="🏢",
    layout="centered"
)

st.title("🏢 건축물 전용면적 조회")
st.write("주소와 동·호수가 포함된 주소를 최대 10개까지 입력하세요.")


# ============================================================
# API KEY
# ============================================================

BUILDING_API_KEY = unquote(
    st.secrets["BUILDING_API_KEY"]
)

JUSO_API_KEY = unquote(
    st.secrets["JUSO_API_KEY"]
)

BUILDING_API_BASE = (
    "https://apis.data.go.kr/1613000/BldRgstHubService"
)

JUSO_API_URL = (
    "https://business.juso.go.kr/addrlink/addrLinkApi.do"
)


# ============================================================
# 문자열 정리
# ============================================================

def normalize_text(value):
    if value is None:
        return ""

    value = str(value).strip()
    value = value.replace(" ", "")
    value = value.replace("　", "")

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
# 주소에서 동 / 호수 추출
# ============================================================

def extract_dong_ho(address):

    text = str(address).strip()

    dong = ""
    ho = ""

    # 101동 1203호
    m = re.search(
        r"(\d+)\s*동\s*(\d+[A-Za-z가-힣]*)\s*호?\s*$",
        text,
        re.IGNORECASE
    )

    if m:
        dong = m.group(1)
        ho = m.group(2)

        text = text[:m.start()].strip()

        return text, dong, ho

    # 101동1203호
    m = re.search(
        r"(\d+)\s*동\s*(\d+[A-Za-z가-힣]*)호\s*$",
        text,
        re.IGNORECASE
    )

    if m:
        dong = m.group(1)
        ho = m.group(2)

        text = text[:m.start()].strip()

        return text, dong, ho

    # 1203호
    m = re.search(
        r"(\d+[A-Za-z가-힣]*)\s*호\s*$",
        text,
        re.IGNORECASE
    )

    if m:
        ho = m.group(1)

        text = text[:m.start()].strip()

        return text, dong, ho

    # 건물명 뒤에 호수만 있는 경우
    # 예: 일성트루엘오피스텔 715
    m = re.search(
        r"\s(\d{2,5})\s*$",
        text
    )

    if m:
        ho = m.group(1)
        text = text[:m.start()].strip()

    return text, dong, ho


# ============================================================
# Juso 주소 검색
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

        response = requests.get(
            JUSO_API_URL,
            params=params,
            timeout=15
        )

        response.raise_for_status()

        data = response.json()

        common = (
            data
            .get("results", {})
            .get("common", {})
        )

        if common.get("errorCode") != "0":

            return None, (
                "주소 API 오류: "
                + str(common.get("errorMessage", ""))
            )

        juso_list = (
            data
            .get("results", {})
            .get("juso", [])
        )

        if not juso_list:

            return None, "도로명주소 검색 결과 없음"

        j = juso_list[0]

        admCd = j.get("admCd", "")

        if len(admCd) < 10:

            return None, "법정동코드 확인 실패"

        sigunguCd = admCd[:5]
        bjdongCd = admCd[5:10]

        jibunAddr = j.get("jibunAddr", "")

        # 지번주소 중 숫자 부분 추출
        # 예:
        # 서울특별시 관악구 신림동 1523-1 일성트루엘
        # → 1523 / 1

        jibun_match = re.search(
            r"\s(\d+)(?:-(\d+))?(?:\s|$)",
            jibunAddr
        )

        if jibun_match:

            bun = jibun_match.group(1)
            ji = jibun_match.group(2) or "0"

        else:

            return None, "지번 추출 실패"

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

    except Exception as e:

        return None, f"주소 API 통신 오류: {e}"


# ============================================================
# 건축물대장 전유부
# ============================================================

def get_expos_info(
    sigunguCd,
    bjdongCd,
    platGbCd,
    bun,
    ji
):

    url = (
        BUILDING_API_BASE
        + "/getBrExposInfo"
    )

    all_items = []

    page_no = 1
    num_rows = 100

    while True:

        params = {
            "serviceKey": BUILDING_API_KEY,
            "sigunguCd": sigunguCd,
            "bjdongCd": bjdongCd,
            "platGbCd": platGbCd,
            "bun": bun,
            "ji": ji,
            "pageNo": page_no,
            "numOfRows": num_rows,
            "_type": "json"
        }

        try:

            response = requests.get(
                url,
                params=params,
                timeout=20
            )

            response.raise_for_status()

            data = response.json()

            body = (
                data
                .get("response", {})
                .get("body", {})
            )

            total_count = int(
                body.get("totalCount", 0)
            )

            items = (
                body
                .get("items", {})
                .get("item", [])
            )

            if isinstance(items, dict):
                items = [items]

            all_items.extend(items)

            if not items:
                break

            if len(all_items) >= total_count:
                break

            page_no += 1

        except Exception:
            break

    return all_items


# ============================================================
# 전유공용면적
# ============================================================

def get_area_info(
    sigunguCd,
    bjdongCd,
    platGbCd,
    bun,
    ji
):

    url = (
        BUILDING_API_BASE
        + "/getBrExposPubuseAreaInfo"
    )

    all_items = []

    page_no = 1
    num_rows = 100

    while True:

        params = {
            "serviceKey": BUILDING_API_KEY,
            "sigunguCd": sigunguCd,
            "bjdongCd": bjdongCd,
            "platGbCd": platGbCd,
            "bun": bun,
            "ji": ji,
            "pageNo": page_no,
            "numOfRows": num_rows,
            "_type": "json"
        }

        try:

            response = requests.get(
                url,
                params=params,
                timeout=20
            )

            response.raise_for_status()

            data = response.json()

            body = (
                data
                .get("response", {})
                .get("body", {})
            )

            total_count = int(
                body.get("totalCount", 0)
            )

            items = (
                body
                .get("items", {})
                .get("item", [])
            )

            if isinstance(items, dict):
                items = [items]

            all_items.extend(items)

            if not items:
                break

            if len(all_items) >= total_count:
                break

            page_no += 1

        except Exception:
            break

    return all_items


# ============================================================
# 전용면적 찾기
# ============================================================

def find_exclusive_area(
    expos_items,
    area_items,
    target_dong,
    target_ho
):

    target_dong = normalize_dong(target_dong)
    target_ho = normalize_ho(target_ho)

    candidates = []

    # --------------------------------------------------------
    # 동 + 호 매칭
    # --------------------------------------------------------

    for item in expos_items:

        item_dong = normalize_dong(
            item.get("dongNm", "")
        )

        item_ho = normalize_ho(
            item.get("hoNm", "")
        )

        if target_ho and item_ho != target_ho:
            continue

        if target_dong:

            if item_dong != target_dong:
                continue

        candidates.append(item)

    # --------------------------------------------------------
    # 동이 없는 경우 호수만으로 재검색
    # --------------------------------------------------------

    if not candidates and target_ho:

        for item in expos_items:

            item_ho = normalize_ho(
                item.get("hoNm", "")
            )

            if item_ho == target_ho:
                candidates.append(item)

    if not candidates:

        return None, None, "전유부에서 동/호 매칭 실패"

    # --------------------------------------------------------
    # 첫 번째 후보
    # --------------------------------------------------------

    candidate = candidates[0]

    pk = str(
        candidate.get(
            "mgmBldrgstPk",
            ""
        )
    )

    matched_dong = candidate.get(
        "dongNm",
        ""
    )

    matched_ho = candidate.get(
        "hoNm",
        ""
    )

    # --------------------------------------------------------
    # 면적자료에서 전유만 찾기
    # --------------------------------------------------------

    area_candidates = []

    for item in area_items:

        item_pk = str(
            item.get(
                "mgmBldrgstPk",
                ""
            )
        )

        if pk and item_pk:

            if item_pk != pk:
                continue

        else:

            item_dong = normalize_dong(
                item.get("dongNm", "")
            )

            item_ho = normalize_ho(
                item.get("hoNm", "")
            )

            if target_dong:
                if item_dong != target_dong:
                    continue

            if target_ho:
                if item_ho != target_ho:
                    continue

        gb = str(
            item.get(
                "exposPubuseGbCdNm",
                ""
            )
        ).strip()

        gb_cd = str(
            item.get(
                "exposPubuseGbCd",
                ""
            )
        ).strip()

        if gb == "전유" or gb_cd == "1":

            area_candidates.append(item)

    if not area_candidates:

        return (
            None,
            pk,
            "전유공용면적에서 전유 항목 없음"
        )

    total_area = 0

    for item in area_candidates:

        try:

            area = float(
                str(
                    item.get(
                        "area",
                        "0"
                    )
                ).replace(",", "")
            )

            total_area += area

        except Exception:
            pass

    if total_area <= 0:

        return (
            None,
            pk,
            "전용면적 값 없음"
        )

    return (
        round(total_area, 2),
        pk,
        "정상"
    )


# ============================================================
# 주소 하나 조회
# ============================================================

def process_address(address):

    original = address.strip()

    if not original:

        return {
            "주소": "",
            "건물명": "",
            "동": "",
            "호": "",
            "전용면적": "",
            "평": "",
            "조회상태": "주소 없음"
        }

    # --------------------------------------------------------
    # 동 / 호 분리
    # --------------------------------------------------------

    base_address, target_dong, target_ho = (
        extract_dong_ho(original)
    )

    # --------------------------------------------------------
    # Juso 검색용 도로명주소 추출
    # --------------------------------------------------------

    # 예:
    # 서울특별시 관악구 신림로23길 16 일성트루엘
    #
    # →
    # 서울특별시 관악구 신림로23길 16

    m = re.match(
        r"^(.+\s+\d+(?:-\d+)?)\s+",
        base_address
    )

    if m:

        juso_search_address = m.group(1).strip()

    else:

        juso_search_address = base_address

    # --------------------------------------------------------
    # Juso
    # --------------------------------------------------------

    juso, msg = search_juso(
        juso_search_address
    )

    if juso is None:

        return {
            "주소": original,
            "건물명": "",
            "동": target_dong,
            "호": target_ho,
            "전용면적": "",
            "평": "",
            "조회상태": msg
        }

    # --------------------------------------------------------
    # 건축물대장
    # --------------------------------------------------------

    platGbCd = "0"

    expos_items = get_expos_info(
        juso["sigunguCd"],
        juso["bjdongCd"],
        platGbCd,
        juso["bun"],
        juso["ji"]
    )

    # 산번지 재검색
    if not expos_items:

        platGbCd = "1"

        expos_items = get_expos_info(
            juso["sigunguCd"],
            juso["bjdongCd"],
            platGbCd,
            juso["bun"],
            juso["ji"]
        )

    if not expos_items:

        return {
            "주소": original,
            "건물명": juso["bdNm"],
            "동": target_dong,
            "호": target_ho,
            "전용면적": "",
            "평": "",
            "조회상태": "건축물대장 전유부 조회 결과 없음"
        }

    # --------------------------------------------------------
    # 전유공용면적
    # --------------------------------------------------------

    area_items = get_area_info(
        juso["sigunguCd"],
        juso["bjdongCd"],
        platGbCd,
        juso["bun"],
        juso["ji"]
    )

    if not area_items:

        return {
            "주소": original,
            "건물명": juso["bdNm"],
            "동": target_dong,
            "호": target_ho,
            "전용면적": "",
            "평": "",
            "조회상태": "전유공용면적 조회 결과 없음"
        }

    # --------------------------------------------------------
    # 면적 매칭
    # --------------------------------------------------------

    area, pk, status = find_exclusive_area(
        expos_items,
        area_items,
        target_dong,
        target_ho
    )

    pyeong = ""

    if area is not None:

        pyeong = round(
            area / 3.305785,
            2
        )

    return {
        "주소": original,
        "건물명": juso["bdNm"],
        "동": target_dong,
        "호": target_ho,
        "전용면적": area if area is not None else "",
        "평": pyeong,
        "조회상태": status
    }


# ============================================================
# 화면
# ============================================================

st.subheader("주소 입력")

st.caption(
    "예: 서울특별시 관악구 신림로23길 16 일성트루엘 715호"
)

address_text = st.text_area(
    "주소를 한 줄에 하나씩 입력하세요.",
    height=250,
    placeholder=(
        "서울특별시 관악구 신림로23길 16 일성트루엘 715호\n"
        "서울특별시 ○○구 ○○로 10 101동 1203호\n"
        "서울특별시 ○○구 ○○로 20 ○○오피스텔 805호"
    )
)

st.caption("최대 10개까지 조회할 수 있습니다.")


# ============================================================
# 조회 버튼
# ============================================================

if st.button(
    "🔍 전용면적 조회",
    type="primary",
    use_container_width=True
):

    addresses = [
        x.strip()
        for x in address_text.splitlines()
        if x.strip()
    ]

    if not addresses:

        st.warning(
            "주소를 한 개 이상 입력해주세요."
        )

    elif len(addresses) > 10:

        st.error(
            "한 번에 최대 10개까지만 조회할 수 있습니다."
        )

    else:

        results = []

        progress = st.progress(0)

        status_text = st.empty()

        for i, address in enumerate(addresses):

            status_text.write(
                f"{i + 1}/{len(addresses)} "
                f"조회 중: {address}"
            )

            result = process_address(address)

            results.append(result)

            progress.progress(
                (i + 1) / len(addresses)
            )

            # API 과도한 호출 방지
            time.sleep(0.3)

        status_text.empty()

        progress.empty()

        st.success(
            f"{len(results)}개 주소 조회가 완료되었습니다."
        )

        # ----------------------------------------------------
        # 결과표
        # ----------------------------------------------------

        result_df = st.session_state.get(
            "result_df",
            None
        )

        import pandas as pd

        result_df = pd.DataFrame(results)

        st.subheader("조회 결과")

        st.dataframe(
            result_df[
                [
                    "주소",
                    "건물명",
                    "동",
                    "호",
                    "전용면적",
                    "평",
                    "조회상태"
                ]
            ],
            use_container_width=True,
            hide_index=True
        )

        # ----------------------------------------------------
        # CSV 다운로드
        # ----------------------------------------------------

        csv_data = result_df.to_csv(
            index=False,
            encoding="utf-8-sig"
        )

        st.download_button(
            "📥 결과 CSV 다운로드",
            data=csv_data,
            file_name="전용면적_조회결과.csv",
            mime="text/csv",
            use_container_width=True
        )
