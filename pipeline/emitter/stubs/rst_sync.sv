// 임시 표준 리셋 동기화 셀. 사내 표준 셀 이름과 포트를 받으면 바꾼다 (P0 자료).
// 비동기 assert(rst_n이 0이면 바로 rst_n_sync = 0), 동기 deassert(rst_n이 1이 된 뒤 clk 상승 에지 두 번째에 1).
module rst_sync (
  input  logic clk,
  input  logic rst_n,
  output logic rst_n_sync
);
  logic s1;
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      s1         <= 1'b0;
      rst_n_sync <= 1'b0;
    end else begin
      s1         <= 1'b1;
      rst_n_sync <= s1;
    end
  end
endmodule
