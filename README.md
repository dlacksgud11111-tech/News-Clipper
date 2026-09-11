# 원전·건설 데일리 뉴스 클리핑

전날 나온 원전·건설 뉴스를 국내외로 훑어 30건으로 추린 뒤, Claude가 중요도를 판단해
상위 10건만 골라 매일 오전 7시 텔레그램으로 보냅니다.

```
[1] 수집              [2] 정제            [3] 1차 선별        [4] 2차 선별       [5] 전송
Google News RSS   →  시간창 필터     →   키워드 스코어링  →  Claude 중요도  →  텔레그램
+ 전문지 RSS          중복 사건 병합      30건                판단 → 10건        봇 메시지
(22개 소스)           차단어 제거
```

실측 기준 한 번 실행에 **원시 기사 약 1,200건 → 시간창 내 700건 → 중복 제거 후 460개 사건
→ 차단어 제거 후 270건 → 숏리스트 30건 → 최종 10건**으로 좁혀집니다.

받아보는 모양은 이렇습니다.

```
■ 원전·건설 Daily News
2026.09.11 (금) | 후보 30건 → 10건

──── 원전 ────
· 정책: SMR 특별법·시행령 11일 시행 — 2027년 민관 합동 상세설계 착수
  원문보기 · 뉴스웍스 외 3곳          ← 파란 글씨, 누르면 원문
· 한수원: 프랑스 오라노와 우라늄 농축 협력 체결 — 밸류체인 해외 파트너십 확대
  원문보기 · World Nuclear News

──── 건설·플랜트 ────
· 삼성E&A: 사우디 비료 프로젝트 EPC 수주 — 35억달러(약 4조7000억원)
  원문보기 · 뉴시스 외 9곳

──── 해외 ────
──── 리스크 ────
```

`태그: 사실 — 부연` 한 줄에 매체 링크 한 줄. 중요도 등급은 따로 매기지 않고,
**블록 안에서 중요한 순서대로** 놓습니다.

---

## 1. 준비물 3가지

| 항목 | 발급처 | 용도 |
|---|---|---|
| 텔레그램 봇 토큰 | [@BotFather](https://t.me/BotFather) | 메시지 전송 |
| 텔레그램 chat_id | 아래 설명 | 받는 사람(나) 지정 |
| Anthropic API 키 | [console.anthropic.com](https://console.anthropic.com/settings/keys) | 중요도 판단 |

### 텔레그램 봇 만들기

1. 텔레그램에서 **@BotFather** 검색 → `/newbot` 입력
2. 봇 이름과 아이디(`_bot`으로 끝나야 함)를 정하면 **토큰**을 줍니다
3. 방금 만든 내 봇을 검색해서 대화방을 열고 **아무 메시지나 하나 보냅니다** ← 이걸 안 하면 봇이 나에게 말을 걸 수 없습니다
4. 브라우저에서 아래 주소를 열어 `chat_id`를 확인합니다 (`<토큰>` 자리에 위 토큰을 넣으세요)

```
https://api.telegram.org/bot<토큰>/getUpdates
```

응답 JSON에서 `"chat":{"id":123456789` 의 숫자가 **chat_id** 입니다.

> 토큰과 API 키는 비밀번호와 같습니다. 아래 GitHub Secrets에 **직접** 입력하시고,
> 코드나 설정 파일에는 절대 적지 마세요.

---

## 2. GitHub Actions에 올리기 (매일 자동 실행)

```bash
git init && git add . && git commit -m "원전·건설 데일리 클리핑"
```

GitHub에서 **비공개(Private) 레포**를 만들고 푸시합니다.

```bash
git remote add origin https://github.com/<내계정>/news-clipper.git
git push -u origin main
```

그다음 레포의 **Settings → Secrets and variables → Actions → New repository secret**에서
아래 3개를 등록합니다. 이름을 정확히 맞춰야 합니다.

| Name | Secret |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic 콘솔에서 받은 키 |
| `TELEGRAM_BOT_TOKEN` | BotFather가 준 토큰 |
| `TELEGRAM_CHAT_ID` | 위에서 확인한 숫자 |

끝입니다. 매일 **한국시간 오전 7시**(UTC 22:00)에 자동 실행됩니다.

바로 확인하고 싶으면 **Actions 탭 → 원전·건설 데일리 클리핑 → Run workflow**로 즉시 돌려볼 수 있습니다.

> GitHub Actions의 cron은 서버가 붐빌 때 5~20분 늦게 실행될 수 있습니다.
> 7시 정각이 중요하면 워크플로의 cron을 `50 21 * * *`(06:50 KST)로 당겨두세요.

---

## 3. 로컬에서 테스트

```bash
pip install -r requirements.txt
```

**전송 없이 결과만 미리보기** — API 키도 필요 없습니다. 수집·스코어링이 잘 되는지 볼 때 쓰세요.

```bash
python clipper.py --dry-run --no-ai
```

**AI 선별까지 포함해서 미리보기** (Anthropic API 키 필요, 텔레그램 전송은 안 함)

```bash
python clipper.py --dry-run
```

**실제 전송 테스트**

Windows PowerShell:

```powershell
$env:ANTHROPIC_API_KEY="..."; $env:TELEGRAM_BOT_TOKEN="..."; $env:TELEGRAM_CHAT_ID="..."; python clipper.py
```

macOS / Linux:

```bash
ANTHROPIC_API_KEY=... TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python clipper.py
```

---

## 4. 입맛에 맞게 고치기 — `config.yaml` 만 만지면 됩니다

### 제목과 받는 개수

```yaml
title: 원전·건설 Daily News   # 메시지 첫 줄
shortlist_size: 30            # AI에게 보여줄 후보 (20~30 권장)
final_size: 10                # 최종으로 받을 개수
```

### 블록(섹터 구분) 바꾸기

기본은 `원전 / 건설·플랜트 / 해외 / 리스크` 4개입니다. 바꾸려면 `clipper.py` 두 군데를
같이 고쳐야 합니다 — 한쪽만 고치면 그 블록의 기사가 통째로 사라집니다.

1. `class Pick`의 `block` 필드 — Claude가 고를 수 있는 이름
2. `BLOCK_ORDER` 리스트 — 메시지에 찍히는 순서
3. `SYSTEM` 프롬프트의 `[블록 배정]` — 어떤 기사를 어디에 넣을지 기준

예를 들어 기자재·밸류체인을 따로 보고 싶으면 세 곳 모두에 `기자재`를 추가하면 됩니다.

### 태그(줄 맨 앞 굵은 글씨)

기업 뉴스면 종목명, 아니면 테마가 자동으로 붙습니다. 규칙은 `SYSTEM` 프롬프트의
`[작성 형식]`에 있습니다. 특정 종목을 항상 같은 이름으로 쓰고 싶으면(예: "한국수력원자력"
말고 늘 "한수원") 거기에 한 줄 적어주세요.

### 키워드 추가

`queries`에 항목을 추가하면 그만큼 소스가 늘어납니다. Google News 검색 문법을 그대로 씁니다.

```yaml
  - name: 원전-국내-신규관심사
    lang: ko
    q: "(원전 부품 OR 주기기 OR 터빈) {window}"
```

- `A OR B` — 둘 중 아무거나
- `"따옴표"` — 정확히 이 문구
- `{window}` — 실행 시 `when:2d`로 자동 치환 (건드리지 마세요)

### 특정 매체만 보기

```yaml
site_queries:
  - name: 매일경제
    lang: ko
    site: mk.co.kr
    q: "(원전 OR 건설 수주)"
```

`site:` 값은 도메인만 적습니다. 국내 건설·에너지 전문지는 자체 RSS를 닫아둔 곳이 많아,
직접 RSS(`feeds`)보다 이 방식이 안정적입니다.

### 원치 않는 기사가 계속 올라올 때

`blocklist`에 단어를 추가하세요. **제목에** 그 단어가 있으면 점수와 무관하게 즉시 버립니다.

```yaml
blocklist:
  - 특징주
  - 유소년       # 스포츠 후원 홍보 기사
  - 모델하우스
```

### 중요도 기준 바꾸기

`scoring.event.words`가 "중요한 사건"의 신호어입니다. 여기에 단어를 넣으면 그런 기사가
숏리스트에 잘 올라옵니다. 최종 선별 기준 자체를 바꾸려면 `clipper.py`의 `SYSTEM` 프롬프트를
고치세요 — 4개 축(수주·계약·실적 / 정책·규제 / 프로젝트·기술 / 리스크·사고)이 여기 적혀 있습니다.

### 문체가 마음에 안 들 때

`SYSTEM` 프롬프트의 `[작성 형식]` 블록이 문체를 결정합니다. 현재는 **명사형 종결**
("수주", "체결", "시행")에 존댓말·서술형 금지입니다. 서술형이 낫다면 그 두 줄만 고치세요.
`detail`(대시 뒤 부연)을 아예 빼고 싶으면 프롬프트에서 "detail은 항상 빈 문자열"이라고
지시하면 됩니다.

### 같은 뉴스가 여러 번 올 때 / 너무 합쳐질 때

```yaml
dedup_threshold: 0.45         # 글자 유사도. 낮출수록 공격적으로 병합
shortlist_word_overlap: 0.40  # 단어 겹침. 낮출수록 주제가 다양해짐
```

`shortlist_word_overlap`을 0.35 아래로 내리면 서로 다른 재건축 수주 건까지 같은 사건으로
합쳐지기 시작합니다. 0.40 근처를 권합니다.

---

## 5. 비용

하루 1회, 후보 30건 기준 대략 **입력 4천 토큰 / 출력 1천~4천 토큰**입니다.
Claude Opus 5 단가($5/$25 per 1M)로 **하루 10~15센트, 월 3~5달러** 수준입니다.

더 줄이고 싶으면 `config.yaml`의 모델을 바꾸세요. 판단 품질은 조금 떨어집니다.

```yaml
model: claude-sonnet-5   # $2/$10 per 1M — 월 1~2달러 수준
```

GitHub Actions는 비공개 레포 기준 월 2,000분 무료이고, 이 작업은 1회에 1~2분이라 무료 범위 안입니다.

---

## 6. 문제가 생기면

| 증상 | 확인할 것 |
|---|---|
| 메시지가 안 옴 | Actions 탭에서 실행 로그 확인. 내 봇에게 먼저 말을 걸었는지(위 1번 3단계) |
| "수집된 기사가 없습니다" | `window_hours`가 너무 짧은지, 네트워크 차단이 있는지 |
| 기사는 오는데 AI 선별이 빠짐 | `ANTHROPIC_API_KEY` Secret 이름 오타 확인 |
| 엉뚱한 기사가 옴 | `blocklist`에 단어 추가, `scoring.topic.words` 조정 |
| 같은 뉴스가 중복 | `shortlist_word_overlap`을 0.38 정도로 낮춰보기 |

로그는 5단계로 찍히므로 어디서 막혔는지 바로 보입니다.

```
[1/5] 수집: 22개 소스
      원시 기사 1220건
[2/5] 시간창 09/10 15:42 ~ 09/11 16:42 → 697건
      중복 제거 → 463개 사건
      차단어로 제외 15건 → 채점 대상 274건
[3/5] 숏리스트 30건 (국내 15 / 해외 15)
[4/5] Claude 중요도 판단 (claude-opus-5)
      선별 10건 | 토큰 in 4300 / out 980
[5/5] 텔레그램 전송 (1개 메시지)
      완료
```
