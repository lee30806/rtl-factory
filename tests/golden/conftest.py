# 원본 테스트벤치(upstream/tb)는 pytest가 직접 모으지 않는다. tests/golden/run_golden.py가 방출 RTL에 붙여 실행한다.
collect_ignore_glob = ["*/upstream/*"]
