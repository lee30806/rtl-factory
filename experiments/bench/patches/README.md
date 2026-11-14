# 벤치마크 데이터셋 보정 파일

## ChipVerilog: `chipverilog_or1200_sb_fifo.v`

ChipVerilog 데이터셋(HKUSTGZ-MICS-LYU/ChipVerilog, master b76b967)의 `Des/or1200/or1200_sb/or1200_sb.v`는 `or1200_sb_fifo`를
인스턴스화하지만 그 모듈 원본이 데이터셋 어디에도 없다. 공식 채점 도구(`tools/formal_equivalence.py`)는 이를 1단 FIFO 스텁으로
대체해 정답을 만들므로(`generated_stub_modules`), 프롬프트가 말하는 4단 Store Buffer FIFO를 맞게 만든 설계일수록 등가에서 멀어진다.

이 파일은 openrisc/or1200(`rtl/verilog/or1200_sb_fifo.v`, LGPL 2.1)의 원본에서 리셋 매크로(`OR1200_RST_EVENT`,
`OR1200_RST_VALUE`)만 데이터셋의 오래된 `or1200_defines.v`에 맞춰 `posedge` / active-high로 바꾼 것이다. 적용:

    cp experiments/bench/patches/chipverilog_or1200_sb_fifo.v build/datasets/ChipVerilog/Des/or1200/or1200_sb/or1200_sb_fifo.v

적용하면 공식 도구가 같은 디렉터리에서 모듈을 찾아 스텁 없이 채점한다. 나머지 미정의 하위 모듈(or1200_spram_*, or1200_dpram_*,
or1200_tpram_32x32, or1200_amultp2_32x32)은 메모리 매크로와 곱셈기라 공식 도구의 동작 모델 스텁이 원본과 기능이 같다고 보고 두었다.
