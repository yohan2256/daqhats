# 일반 음압레벨·실간 차음·외벽/창호 측정

PC 메뉴 **Measurement modes → Sound level / airborne / facade**에서 엽니다.
Pi 연결과 스캔 시작은 기존 메인 화면에서 수행합니다. 새 창은 별도 측정 모드이며
기존 바닥충격음 세션을 변경하지 않습니다. 실행 중인 측정/녹음이 끝난 뒤 여세요.
새 기능은 PC에서 원시 파형을 분석하므로 Pi 실시간 필터 부하를 추가하지 않습니다.

## 일반 음압레벨

General sound level에서 채널(화면 번호 1부터), 시간, Fast/Slow와 옥타브 해상도를
선택하고 Capture를 누릅니다. 실제 10초 선행 이력이 확보되면 Capture now가
표시되고 측정 시간이 시작됩니다. A/C/Z 각각 Leq, Lmax, Lmin, Lpeak, SEL 및
L10/L50/L90을 계산합니다. 최댓값과 피크는 모든 샘플에서 검출합니다.
LN과 시간 이력은 약 10 ms 간격이며 그래프는 첫 선택 채널의 A 가중 레벨입니다.
일반 대역 분석은 Z 가중 31.5–8000 Hz, 1/3 또는 1/1 옥타브입니다.
A/C 필터는 서버와 동일한 아날로그 극점의 bilinear 구현이며 장비 전체의
IEC 61672 Class 적합성이 검증되었다는 의미는 아닙니다.

결과는 서버 raw snapshot 끝을 기준으로 한 지정 시간 구간입니다. 선행 이력은
평가 시간에서 제외됩니다. Save last WAV는 분석에 사용한 선행 이력 포함 파형과
채널/단위 정보를 저장합니다. JSON은 모든 기록과 10 ms 이력을, CSV는 대역값·
음압 지표·시간 이력·계산된 차음 결과를 내보냅니다. 연속 무기한 로깅 모드는
아니며 1회 측정은 최대 240초, 선행 이력 포함 길이가 Pi 버퍼 이내여야 합니다.

## 실간 차음

Room-to-room airborne에서 Source ID별로 L1(음원실), L2(수음실),
B2(음원 정지 후 수음실 배경소음)를 기록합니다. 마이크를 이동할 때 Position을
바꾸세요. 여러 채널은 별도 위치로 기록됩니다. 같은 위치 ID를 중복 저장하면
계산이 거부되므로 재측정 시 이전 기록을 삭제하세요.

T 단계는 외부 소음원을 안정적으로 재생한 뒤 Capture now 신호에 맞춰 소음을
정지시키는 감쇠 기록입니다. 기존 T20 분석기의 품질 조건을 통과한 16개 대역만
완전한 T 기록으로 받아들입니다. 여러 T 기록은 산술 평균합니다. 외부 측정한
T60은 오른쪽 T 열에 직접 입력할 수도 있습니다. 다른 측정 모드의 T를 자동으로
복사하지 않습니다. 내장 음원 재생 기능은 메인 프로그램에 있으며, 이 창에서는
외부 소음원 운용 또는 사전 설정된 음원을 사용합니다.

각 위치의 대역 스펙트럼을 왼쪽 열에 입력하고 Add entered spectrum으로
L1/L2/B2/T 기록을 추가할 수도 있습니다. 수동 입력은 원시 측정과 구분됩니다.

수음실 T가 있으면 DnT, 추가로 실 체적과 시험체 면적이 있으면 R′를 계산합니다.
공간 평균은 음압 에너지 평균, 서로 다른 음원 위치는 각 위치의 투과비 평균으로
합칩니다. 일부 대역이나 음원 위치가 누락되면 계산하지 않습니다.

## 외벽 전체와 창호/부재

- Whole facade — loudspeaker: 실외 **2 m** L1, 실내 L2/B2/T → Dls,2m,nT.
- Whole facade — road traffic: **PAIR**로 실외 2 m 기준 채널과 실내 채널을
  같은 장치·같은 샘플 구간에서 측정 → Dtr,2m,nT. 독립 장치 사이의 동시성이
  검증되지 않아 장치 간 PAIR를 거부합니다. 이벤트마다 다른 Source/event ID를
  사용하고 해당 ID에 배경 기록을 추가합니다. 항공/철도 단일사건 방식은 제외합니다.
- Window / element — 45° loudspeaker: 실외 **표면 마이크**, 45° 입사 스피커,
  시험체 면적·수음실 체적·T → R′45°. 2 m 값으로 대체하지 마세요.
  현장 겉보기 성능이며 측로전달을 포함하고 실험실 창호 Rw로 표시하지 않습니다.

기본 차음 평가 범위는 **100–3150 Hz, 16개 1/3 옥타브**입니다.
ISO 717-1:2020 기준 곡선 이동과 기본 C/Ctr를 계산합니다. 50–80 Hz의 작은 방
모서리 측정 절차와 확장 C/Ctr는 이 버전에 포함하지 않습니다. 배경과의 차이가
6 dB 미만이면 보정된 결과는 **차음성능 하한값**으로 표시하며 합격을 선언하지
않습니다. 6–10 dB는 에너지 차감, 10 dB 이상은 배경 보정 생략입니다.

계산 구현과 현장 규격 적합성은 별개입니다. 마이크/스피커 배치, 실외 반사와
입사각, 기상, 음원 안정성, 공간 표본 수, 교정·측정 불확도는 현장에서 확인해야
합니다. 제한된 위치만 측정한 값은 예비 계산으로 사용하세요. 법적 등급이나
공인 성적서 적합성을 자동 판정하지 않습니다.

## 계산 근거

- ISO 717-1:2020, Tables 3/4, reference shifting and basic C/Ctr:
  https://www.iso.org/standard/77435.html
  https://standards.iteh.ai/catalog/standards/cen/f519cc17-b1ef-4fc8-a085-2bab587bff0a/en-iso-717-1-2020
- ISO 16283-1, room-to-room field measurement:
  https://www.iso.org/standard/55997.html
- ISO 16283-3:2016, facade global and element quantities:
  https://www.iso.org/standard/59748.html
  https://cdn.standards.iteh.ai/samples/59748/8e0e9b3e0f2b4ee9b2b59fc1b33a627f/ISO-16283-3-2016.pdf
- Santos et al., ICSV22 (2015), source-position averaging in ISO 16283-1:
  https://www.researchgate.net/publication/280254241_COMPARING_RESULTS_OF_USING_ISO_140-41998_TO_ISO_16283-12014

추가 의존성은 없으며, 실제 장비 성능 검증은 별도로 필요합니다.
