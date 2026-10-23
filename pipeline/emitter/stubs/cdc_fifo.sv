// 임시 표준 비동기 FIFO 셀. 사내 표준 셀 이름과 포트를 받으면 바꾼다 (P0 자료).
// valid/ready 양쪽 끝. 포인터는 DEPTH의 두 배까지 세는 이진 카운터와 그 gray 코드 레지스터이고,
// 상대편 gray 포인터를 2단으로 동기화해(wq1, wq2 / rq1, rq2) 가득 참과 비어 있음을 정한다.
// 받는 쪽 데이터는 읽기 포인터가 가리키는 칸을 조합으로 읽는다. 리셋은 양쪽 모두 active-low이고(ASYNC_RST=1이면 비동기,
// 0이면 동기. 설계의 리셋 방식을 따름),
// 한쪽만 리셋하면 포인터가 어긋나므로 두 리셋을 함께 건다. DEPTH는 2 이상인 2의 거듭제곱.
module cdc_fifo #(
  parameter int WIDTH = 1,
  parameter int DEPTH = 2,
  parameter bit ASYNC_RST = 1'b1
) (
  input  logic             src_clk,
  input  logic             src_rst_n,
  input  logic             src_valid,
  output logic             src_ready,
  input  logic [WIDTH-1:0] src_data,
  input  logic             dst_clk,
  input  logic             dst_rst_n,
  output logic             dst_valid,
  input  logic             dst_ready,
  output logic [WIDTH-1:0] dst_data
);
  localparam int A = $clog2(DEPTH);
  localparam logic [A:0] FULL = (A + 1)'(DEPTH);
  localparam logic [A:0] ONE = (A + 1)'(1);

  logic [WIDTH-1:0] mem [DEPTH];
  logic [A:0] wbin, wgray, rbin, rgray, wq1, wq2, rq1, rq2;
  logic [A:0] wnext, rnext, rq2_bin;

  function automatic logic [A:0] gray2bin(input logic [A:0] g);
    for (int i = 0; i <= A; i++) gray2bin[i] = ^(g >> i);
  endfunction

  assign wnext = wbin + ONE;
  assign rnext = rbin + ONE;
  assign rq2_bin = gray2bin(rq2);
  assign src_ready = (wbin - rq2_bin) != FULL;
  assign dst_valid = rgray != wq2;
  assign dst_data = mem[rbin[A-1:0]];

  if (ASYNC_RST) begin : g_src_async
    always_ff @(posedge src_clk or negedge src_rst_n) begin
      if (!src_rst_n) begin
        wbin  <= '0;
        wgray <= '0;
        rq1   <= '0;
        rq2   <= '0;
      end else begin
        rq1 <= rgray;
        rq2 <= rq1;
        if (src_valid && src_ready) begin
          wbin  <= wnext;
          wgray <= wnext ^ (wnext >> 1);
        end
      end
    end
  end else begin : g_src_sync
    always_ff @(posedge src_clk) begin
      if (!src_rst_n) begin
        wbin  <= '0;
        wgray <= '0;
        rq1   <= '0;
        rq2   <= '0;
      end else begin
        rq1 <= rgray;
        rq2 <= rq1;
        if (src_valid && src_ready) begin
          wbin  <= wnext;
          wgray <= wnext ^ (wnext >> 1);
        end
      end
    end
  end

  always_ff @(posedge src_clk) begin
    if (src_valid && src_ready) mem[wbin[A-1:0]] <= src_data;
  end

  if (ASYNC_RST) begin : g_dst_async
    always_ff @(posedge dst_clk or negedge dst_rst_n) begin
      if (!dst_rst_n) begin
        rbin  <= '0;
        rgray <= '0;
        wq1   <= '0;
        wq2   <= '0;
      end else begin
        wq1 <= wgray;
        wq2 <= wq1;
        if (dst_valid && dst_ready) begin
          rbin  <= rnext;
          rgray <= rnext ^ (rnext >> 1);
        end
      end
    end
  end else begin : g_dst_sync
    always_ff @(posedge dst_clk) begin
      if (!dst_rst_n) begin
        rbin  <= '0;
        rgray <= '0;
        wq1   <= '0;
        wq2   <= '0;
      end else begin
        wq1 <= wgray;
        wq2 <= wq1;
        if (dst_valid && dst_ready) begin
          rbin  <= rnext;
          rgray <= rnext ^ (rnext >> 1);
        end
      end
    end
  end
endmodule
