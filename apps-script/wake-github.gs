/**
 * 뉴스 클리핑 — 구글 앱스 스크립트 트리거
 *
 * 하는 일은 딱 하나입니다. 매일 새벽, GitHub Actions 워크플로를 깨웁니다.
 *
 * 왜 이게 필요한가
 *   GitHub의 예약(cron)은 그날 한 번도 실행되지 않을 수 있습니다.
 *   (2026-09-15 실측: 예약 13칸 중 1칸만 실행 → 그날 뉴스가 안 감)
 *   구글 트리거는 구글 서버에서 돌기 때문에 PC가 꺼져 있어도 매일 뜹니다.
 *
 * 왜 깨우는 시각은 대충이어도 되는가
 *   wait=true 로 깨우면 워크플로가 발송 시각(07:00 KST)까지 스스로 기다립니다.
 *   그러니 이 트리거는 07:00 전이기만 하면 05:03이든 05:47이든 상관없습니다.
 *   정각 맞추기는 GitHub 쪽 대기 장치가 초 단위로 합니다.
 *
 * 설치 순서는 README 를 보세요. 요약하면
 *   1) 스크립트 속성에 GITHUB_TOKEN 저장
 *   2) testNow 실행 → GitHub Actions 에 실행이 뜨는지 확인
 *   3) setupTrigger 실행 → 매일 자동
 */

const OWNER = 'dlacksgud11111-tech';
const REPO = 'News-Clipper';
const TRIGGER_HOUR = 5; // KST 05시대에 한 번 (05:00~06:00 사이 임의 시각)

// 깨울 워크플로 목록. 각자 발송 시각까지 스스로 기다리므로, 여기서는
// 그냥 둘 다 깨워 두면 됩니다. 한쪽이 실패해도 다른 쪽은 깨웁니다.
const WORKFLOWS = [
  { file: 'daily-clipping.yml', label: '뉴스 클리핑 (07:00)' },
  { file: 'movers.yml', label: '해외 급등락 (07:30)' },
];

/** 매일 트리거가 부르는 함수입니다. */
function wakeGithub() {
  wakeAll(true); // wait=true → 각자 발송 시각까지 기다렸다 발송
}

/**
 * 워크플로를 차례로 깨웁니다.
 *
 * 하나가 실패해도 나머지는 계속 깨웁니다. 클리핑 쪽 호출이 실패했다고
 * 주가 알림까지 같이 죽으면 안 되니까요. 실패한 것이 있으면 마지막에
 * 예외를 던져서 구글이 실패 알림 메일을 보내게 합니다.
 */
function wakeAll(wait) {
  const failed = [];
  for (var i = 0; i < WORKFLOWS.length; i++) {
    try {
      dispatch(wait, WORKFLOWS[i].file);
      console.log('깨움: ' + WORKFLOWS[i].label);
    } catch (e) {
      console.error(WORKFLOWS[i].label + ' 깨우기 실패 — ' + e.message);
      failed.push(WORKFLOWS[i].label + ': ' + e.message);
    }
  }
  if (failed.length) {
    throw new Error('일부 워크플로를 깨우지 못했습니다
' + failed.join('
'));
  }
}

/**
 * 설치 직후 손으로 한 번 실행해 연결을 확인하는 용도입니다.
 * 오늘 몫이 이미 나갔다면 워크플로가 "이미 발송했습니다"로 즉시 끝나므로
 * 텔레그램에 중복으로 오지 않습니다.
 */
function testNow() {
  wakeAll(true);
}

/**
 * GitHub 에 workflow_dispatch 를 보냅니다.
 *
 * inputs 값의 자료형은 GitHub 쪽에서 문자열만 받던 시절과 불리언도 받는
 * 지금이 섞여 있습니다. 어느 쪽이 통할지 확인할 방법이 없으므로 문자열로
 * 먼저 시도하고, 거절(422)당하면 불리언으로 한 번 더 시도합니다.
 */
function dispatch(wait, workflow) {
  const token = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  if (!token) {
    throw new Error('스크립트 속성에 GITHUB_TOKEN 이 없습니다. 프로젝트 설정 > 스크립트 속성에서 추가하세요.');
  }

  const url = 'https://api.github.com/repos/' + OWNER + '/' + REPO +
              '/actions/workflows/' + workflow + '/dispatches';

  const attempts = [String(wait), wait]; // "true" → true 순서로 시도
  let last = null;

  for (var i = 0; i < attempts.length; i++) {
    const res = UrlFetchApp.fetch(url, {
      method: 'post',
      contentType: 'application/json',
      headers: {
        Authorization: 'Bearer ' + token,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
      },
      payload: JSON.stringify({ ref: 'main', inputs: { wait: attempts[i] } }),
      muteHttpExceptions: true,
    });

    const code = res.getResponseCode();
    if (code === 204) {
      console.log(workflow + ' 를 깨웠습니다 (wait=' + JSON.stringify(attempts[i]) +
                  '). 발송은 그 워크플로가 정한 시각에 이뤄집니다.');
      return;
    }
    last = code + ' ' + res.getContentText();
    console.warn('시도 ' + (i + 1) + ' 실패 (HTTP ' + code + ') — ' + res.getContentText());
  }

  // 여기까지 오면 둘 다 실패한 것입니다. 예외를 던져야 구글이 실패 알림
  // 메일을 보내주므로, 조용히 끊기는 상황을 막을 수 있습니다.
  throw new Error(
    'GitHub 호출이 모두 실패했습니다: ' + last + '\n' +
    '토큰이 만료됐거나 Actions 권한(Read and write)이 빠졌을 수 있습니다.'
  );
}

/** 트리거를 등록합니다. 설치할 때 한 번만 실행하세요. 다시 실행해도 중복되지 않습니다. */
function setupTrigger() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'wakeGithub') ScriptApp.deleteTrigger(t);
  });

  ScriptApp.newTrigger('wakeGithub')
    .timeBased()
    .atHour(TRIGGER_HOUR)
    .everyDays(1)
    .inTimezone('Asia/Seoul')
    .create();

  console.log('트리거 등록 완료 — 매일 KST ' + TRIGGER_HOUR + '시대에 GitHub 을 깨웁니다.');
}

/** 지금 등록된 트리거를 확인합니다. */
function checkTrigger() {
  const ts = ScriptApp.getProjectTriggers();
  if (!ts.length) {
    console.log('등록된 트리거가 없습니다. setupTrigger 를 실행하세요.');
    return;
  }
  ts.forEach(function (t) {
    console.log('트리거: ' + t.getHandlerFunction() + ' / ' + t.getEventType());
  });
}

/** 트리거를 지웁니다. 필요할 때만 쓰세요. */
function removeTrigger() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'wakeGithub') ScriptApp.deleteTrigger(t);
  });
  console.log('트리거를 제거했습니다.');
}
