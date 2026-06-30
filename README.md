# 모여 (FamTrack) - 가족 위치공유 PWA

가족끼리만 사용하는 위치 공유 앱. 버튼을 눌러서 다른 가족 구성원의 최근 위치를 확인하고,
학교/집 같은 장소에 등록해두면 도착/출발 시 알림을 받습니다.

## 구조

- `server.py` : Flask 백엔드 (SQLite). 가족 그룹/멤버 관리, 위치 저장, 지오펜스 판정.
- `static/index.html` : 단일 파일 PWA 프론트엔드 (Leaflet 지도 + OpenStreetMap 타일).
- `static/manifest.json`, `static/sw.js` : PWA 설치 및 서비스워커.
- `requirements.txt` : Flask, flask-cors, gunicorn.

## 동작 방식

1. 한 명이 "가족 그룹 만들기"로 그룹을 생성하면 6자리 가족 코드가 발급됩니다.
2. 나머지 가족은 "코드로 참여하기"에 코드를 입력해 같은 그룹에 들어갑니다.
3. 각자 앱이 켜져 있는 동안 브라우저 Geolocation API로 위치를 주기적으로(약 5분 간격 + 이동 감지 시) 서버에 전송합니다.
4. "위치 공유 확인하기" 버튼을 누르면 가족 전원의 가장 최근 위치를 지도에서 확인합니다.
5. 설정에서 등록한 장소(학교, 집 등)에 누군가 들어오거나 나가면 토스트 알림이 표시됩니다.

## 로컬 실행

```bash
pip install -r requirements.txt
python3 server.py
```

`http://localhost:5000` 접속.

## 배포 (Render.com)

- Build Command: `pip install -r requirements.txt`
- Start Command: `gunicorn server:app`
- Render 무료 티어는 일정 시간 미사용 시 슬립되므로, realspy처럼 UptimeRobot으로 주기적 핑을 권장합니다.

## 한계 (PWA 특성)

- 브라우저가 백그라운드로 가면 위치 추적이 끊길 수 있습니다 (네이티브 앱과 달리 OS 레벨 백그라운드 위치 추적이 제한적).
- 따라서 "버튼을 눌렀을 때 최신 캐시된 위치를 보여주는" 방식 + 주기적 백그라운드 전송을 절충했습니다.
- 완전히 안정적인 백그라운드 지오펜싱이 필요하면 네이티브 앱(Android/iOS) 전환이 필요합니다.

## 개인정보 안내

이 앱은 가족 구성원 본인이 직접 앱을 설치하고 위치 공유에 동의한 경우에만 동작합니다.
타인의 동의 없는 위치 추적 용도로 사용하지 마세요.
