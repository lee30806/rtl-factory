// P11 시험용 원본: top이 직접 open-drain으로 구동하는 1비트 패드 (I2C SDA 꼴)
module od_ctrl (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       sda_i,
    output reg        sda_oe,
    output reg  [7:0] shift
);
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            sda_oe <= 1'b0;
            shift  <= 8'd0;
        end else begin
            shift  <= {shift[6:0], sda_i};
            sda_oe <= shift[3] ^ shift[0];
        end
    end
endmodule

module od_top (
    input  wire       clk,
    input  wire       rst_n,
    inout  wire       sda,
    output wire [7:0] shift
);
    wire sda_oe;
    wire sda_in;
    assign sda = sda_oe ? 1'b0 : 1'bz;
    assign sda_in = sda;
    od_ctrl u_ctrl (.clk(clk), .rst_n(rst_n), .sda_i(sda_in), .sda_oe(sda_oe), .shift(shift));
endmodule
