# Wave 1 Worklist — 배관·승격 전수 (2026-07-24 정합 감사)

근거: scenario-redesign-tracker.md 판정 × catalog.json(68)·controllers.json(live 35) 전수 대조.
분해 검산: 68 = KEEP 14 + FIX 27 + CUT잔존 23 + 신규승격 4(F17-R·F18-P·F19-P/S). live 35 = KEEP 14 + FIX 10 + CUT 7 + 신규 4.

## A. CUT 제거 대상 — 22종 (Task #2)

**판정 번복 1건**: ~~F02-P~~ — 07-23 CUT(근거단절: menus 95행) → **07-24 시드 보강 후 재승격**(46d3371, live). CUT 목록에서 제외, 유지.

제거 22종:
- 음성 13: F01-G F02-G F03-G F04-G F05-G F06-G F07-G F09-G F10-G F11-G F12-G F13-G F14-G
- 근거단절/수단없음 6: F07-R F08-R F11-H F11-P F12-R F12-P (F12-R/P는 F27-R/P가 승계)
- FIX강등 3: F02-R(인덱스 드롭 물리적 무효과) F03-R(leak 경로 없음) F14-H(주입=§2-A 위배)

이 중 **현재 live인 6종**: F01-G F02-R F03-G F05-G F06-G F11-G → live 목록·controllers 블록도 제거.
제거 범위: catalog / controllers(live+블록) / profiles allowed_scenarios·tag_pattern·scenario_parameters·scenario_levels / scenario-metadata / manifests / 테스트 기대값 / executor 상수(해당 시) / execution-matrix 행은 "CUT(사유)" 표기로 갱신(행 삭제 대신 이력 보존).

## B. 이미 승격(live) — 29종: 작업 없음(스모크만)
KEEP 14 전부 + FIX 10(F03-H F03-P F05-H F05-P F05-R F07-H F08-P F09-H F09-P F09-R) + F02-P + 신규 4.
단, FIX 10은 Task #3의 정답구조화·재앵커 반영 대상.

## C. FIX인데 미-live 17종 — 개별 배관 판정
- **경량(정답만, ④✅)**: F02-H(ClassB/disk, injector 실재) F10-R/H/P(injector 실재·좌표정정 2건) F15-P(merge) F15-T3/T4(timeline 결속 계약=3등급 요소 포함)
- **배관 필요**: F04-H(k8s.env unblock) F04-P(rate injector 🟡) F06-P·F15-H/T2(food429→PG502 재정의, F19-S 메커니즘 공유) F13-H/P/R(WebURL 오배정 — **재설계 대기, wave1 제외**)
- **3등급 이관**: F14-P/R(유실 injector·fault-proxy)

## D. 신규 미등록 — 배관·승격 대기
- 신규7 잔여 3: F16-H(Task #4) F17-P(Task #8) F19-Q(3등급: 스케줄러 훅)
- 2차 8: F20×3(Task #5) F21×2(Task #6) F24-Q(Task #7) F23-R(Task #9) F22-P(3등급: hot-account surge)
- Class B 10: Task #10 (F25-H 최선두). F26 drain·F27-R tc·F25-R bare 커넥션은 3등급.

## E. Wave 1 완료 정의
- CUT 22 제거 → catalog 46 기반
- 1·2등급 배관·승격 완료분 전수 + 테스트/교차검증 green
- Task #11: 109 전체 일관 배포 + 승격 전수 실주입 스모크
- 3등급(F19-Q F22-P F14-P/R F26-R/H/P F27-R F25-R F13×3 F03-H소스) = Wave 2
