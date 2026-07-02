import sys, os, re, time
from pathlib import Path
from dotenv import load_dotenv
import google.generativeai as genai

load_dotenv(Path(__file__).parent / ".env")

ARCHIVE_ROOT  = Path(r"G:\내 드라이브\naver_archive")
INSIGHTS_ROOT = Path(__file__).parent / "insights"
SAMPLE = "--sample" in sys.argv

# ── 모델 우선순위: 무료 티어 안정적인 순서 ────────────────────────────
# 2.0-flash: 무료 티어 검증됨, 빠름  /  2.5-flash: 더 스마트하지만 쿼터 빡빡
_MODELS = ["gemini-2.0-flash", "gemini-2.5-flash", "gemini-1.5-flash"]
_QUOTA  = ("429", "quota", "exceeded", "resource_exhausted", "depleted", "rate_limit")

# ── 무료 티어 속도 제한 ────────────────────────────────────────────────
# 키당 15 RPM 한도 → 4.1초 간격(14.6 RPM)으로 안전 마진 확보
# 2개 키 교번 시 실질 29 RPM (속도 2배)
_CALL_INTERVAL = 4.1   # 초/키
_RPD_LIMIT     = 1400  # 일일 안전 한도 (실제 1,500)
_last_called: dict[str, float] = {}
_daily_count: dict[str, int]   = {}


def _keys():
    out = []
    k = os.environ.get("GEMINI_API_KEY", "").strip()
    if k and k.startswith("AIzaSy"): out.append(k)
    for i in range(2, 10):
        k = os.environ.get(f"GEMINI_API_KEY_{i}", "").strip()
        if not k: break
        if k.startswith("AIzaSy"):  # "AQ." 등 비정상 키 자동 제외
            out.append(k)
    return out


def _wait(key: str):
    """키별 최소 호출 간격 보장 + 일일 카운터 증가."""
    now = time.monotonic()
    gap = _CALL_INTERVAL - (now - _last_called.get(key, 0))
    if gap > 0:
        time.sleep(gap)
    _last_called[key] = time.monotonic()
    _daily_count[key] = _daily_count.get(key, 0) + 1
    if _daily_count[key] % 100 == 0:
        print(f"  [Gemini] 오늘 {_daily_count[key]}회 호출 (키: {key[:8]}…)")


def _call(prompt, keys, max_tokens=2000):
    """
    키 교번 방식으로 Gemini 호출.
    - 429 발생 시: 30초 대기 후 다음 키/모델로 전환
    - 일일 1,400회 초과 키는 건너뜀
    """
    if not keys:
        return None

    # 키 × 모델 조합 목록 (교번: key1·model1, key2·model1, key1·model2, ...)
    order = [(keys[k_i % len(keys)], m)
             for m in _MODELS
             for k_i in range(len(keys))]

    backoff = 30.0
    for key, model in order:
        if _daily_count.get(key, 0) >= _RPD_LIMIT:
            print(f"  [Gemini] {key[:8]}… 일일 한도 도달, 건너뜀")
            continue
        try:
            _wait(key)
            genai.configure(api_key=key)
            resp = genai.GenerativeModel(model).generate_content(
                prompt,
                generation_config={"max_output_tokens": max_tokens, "temperature": 0.3},
                request_options={"timeout": 15},  # 120→15: 무효 키 gRPC 행업 방지
            )
            text = getattr(resp, "text", "") or ""
            if text.strip():
                return text.strip()
        except Exception as e:
            err = str(e).lower()
            if any(x in err for x in _QUOTA):
                print(f"  [Gemini] 쿼터 초과 ({model}) → {backoff:.0f}초 대기 후 재시도")
                time.sleep(backoff)
                backoff = min(backoff * 2, 120)
            # 그 외 오류도 다음 조합으로 계속 시도
    return None


def analyze_post(md_text, keys):
    return _call(f"""다음은 투자/경제 블로그 또는 카페 글입니다. 한국어로 간결하게 분석하세요.

---
{md_text[:3000]}
---

형식:
## 핵심 주장
(1~3줄)

## 근거/논리 구조
(저자가 주장을 뒷받침하기 위해 사용한 근거)

## 사고 패턴
(이 글에서 드러나는 저자의 특징적인 사고 방식)
""", keys)


def synthesize_frame(author, analyses, keys):
    sample = "\n\n---\n\n".join(analyses[:30])
    return _call(f""""{author}"가 작성한 여러 글 분석 결과입니다. 종합 사고 프레임을 도출하세요.

{sample}

형식:
## 투자/사고 철학
## 반복되는 사고 패턴
## 자주 사용하는 프레임
## 강점과 주의할 점
""", keys, max_tokens=3000)


def author_name(folder_name):
    return folder_name.split("_")[0]


def main():
    keys = _keys()
    if not keys:
        print("GEMINI_API_KEY가 .env에 없습니다."); sys.exit(1)

    INSIGHTS_ROOT.mkdir(exist_ok=True)

    for source_dir in sorted(ARCHIVE_ROOT.iterdir()):
        if not source_dir.is_dir():
            continue
        author = author_name(source_dir.name)
        print(f"\n{'='*50}\n분석: {author}\n{'='*50}")

        author_dir = INSIGHTS_ROOT / author
        author_dir.mkdir(exist_ok=True)

        md_files = sorted(source_dir.rglob("*.md"))
        if SAMPLE:
            md_files = md_files[:1]

        analyses, total = [], len(md_files)
        for i, md_file in enumerate(md_files, 1):
            out_file = author_dir / md_file.name
            if out_file.exists():
                analyses.append(out_file.read_text(encoding="utf-8"))
                print(f"  [{i}/{total}] ↩ {md_file.name[:45]}")
                continue

            md_text = md_file.read_text(encoding="utf-8")
            if len(md_text.strip()) < 100:
                print(f"  [{i}/{total}] ↩ 내용 없음")
                continue

            print(f"  [{i}/{total}] 분석 중: {md_file.name[:40]}", end="", flush=True)
            result = analyze_post(md_text, keys)
            if not result:
                print(" ✗"); continue

            # 원문 헤더(메타) + 분석 결과
            header_m = re.search(r"^(.*?---)", md_text, re.DOTALL)
            header = header_m.group(1) if header_m else md_text[:300]
            out_file.write_text(f"{header}\n\n{result}\n", encoding="utf-8")
            analyses.append(result)
            print(" ✓")

        # 저자별 종합 프레임
        if analyses and not SAMPLE:
            frame_file = INSIGHTS_ROOT / f"{author}_프레임.md"
            if not frame_file.exists():
                print(f"\n  {author} 종합 프레임 생성 중...", end="", flush=True)
                frame = synthesize_frame(author, analyses, keys)
                if frame:
                    frame_file.write_text(f"# {author} 사고 프레임\n\n{frame}\n", encoding="utf-8")
                    print(" ✓")
                else:
                    print(" ✗")

    print(f"\n완료 → {INSIGHTS_ROOT.resolve()}")


if __name__ == "__main__":
    main()
