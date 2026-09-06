# warehouse-team

Agentic Mini Keyboard Keyring Factory — 스마트 창고팀

## 구조 (v2, 2026-09-05 확정 기준)

- `firmware/Uno_Storage/` — 선반 아두이노(R4 WiFi) 4대, HiveMQ MQTT 직접 구독
- `robot_arm/` — PC 로봇팔 제어 (Dobot Magician Lite ×4 + 컨베이어 ×3, Mobius HTTP 구독)

ESP32 총괄 구조는 9/5부로 폐기됨. 이전 코드는 태그
`archive/pre-cleanup-2026-09` 에서 확인 가능.

## 브랜치 규칙

- 팀원 각자 자기 이름으로 브랜치 생성 후 작업
- 작업 완료 후 main으로 PR
- 예: nohyeonwoo, kimXX, leeXX

## 처음 시작하기

```bash
git clone https://github.com/uyeonim/warehouse-team.git
cd warehouse-team
git checkout -b <본인이름>
```

각자 담당 폴더 밑에서 작업:
- 아두이노 담당 → `firmware/Uno_Storage/`
- 로봇팔 담당 → `robot_arm/`

## 작업 중 수정 후 업데이트하는 방법

먼저 로컬에 클론해둔 저장소 폴더로 이동 (Git Bash 새로 켰거나 다른 폴더에 있었다면 필수):

```bash
cd 저장소_클론한_경로/warehouse-team
```

파일 수정할 때마다 아래 3단계 반복하면 됨.

```bash
# 1. 뭐가 바뀌었는지 확인
git status

# 2. 바뀐 파일 스테이징 + 커밋
git add .
git commit -m "무엇을 왜 바꿨는지 한 줄로"

# 3. 내 브랜치에 push
git push origin <본인이름>
```

## 최초 push (브랜치 처음 올릴 때만)

```bash
git push -u origin <본인이름>
```

`-u`로 한 번 연결해두면 다음부터는 `git push`만 쳐도 됨.

## 다른 사람이 main에 올린 최신 내용 받아오기

```bash
cd 저장소_클론한_경로/warehouse-team

git checkout main
git pull

git checkout <본인이름>
git merge main
```

## 작업 끝나면 main으로 PR

1. GitHub 저장소 페이지 접속
2. `Compare & pull request` 버튼 클릭 (push 직후 자동으로 뜸)
3. 내용 확인 후 PR 생성 → 리뷰 후 merge
