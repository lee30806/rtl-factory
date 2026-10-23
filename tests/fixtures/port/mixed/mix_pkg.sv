// P11 시험용 package: 상수와 상태 enum
package mix_pkg;
  localparam int K = 3;
  typedef enum logic [1:0] {S_IDLE, S_RUN, S_DONE} st_t;
endpackage
