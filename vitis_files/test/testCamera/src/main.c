#include <string.h>

#include "sleep.h"
#include "xaxivdma.h"
#include "xaxivdma_hw.h"
#include "xiicps.h"
#include "xil_cache.h"
#include "xil_io.h"
#include "xil_mmu.h"
#include "xil_printf.h"
#include "xparameters.h"
#include "xstatus.h"

#define I2C_CLK_RATE_HZ        100000U
#define OV7670_SCCB_ADDR       0x21U

#define FRAME_WIDTH            640U
#define FRAME_HEIGHT           480U
#define BYTES_PER_PIXEL        4U
#define FRAME_STRIDE_BYTES     (FRAME_WIDTH * BYTES_PER_PIXEL)
#define FRAME_BYTES            (FRAME_STRIDE_BYTES * FRAME_HEIGHT)
#define FRAME_COUNT            3U

#define FRAME_BUF_BASE         0x10000000U
#define FRAME_BUF_TOTAL_BYTES  (FRAME_BYTES * FRAME_COUNT)
#define FRAME_BUF_FILL_WORD    0xA5A5A5A5U

#define ENABLE_OV7670_COLORBAR 0
#define STATUS_POLL_COUNT      10U
#define VDMA_SETTLE_US         200000U
#define VDMA_S2MM_W1C_MASK     (XAXIVDMA_SR_ERR_ALL_MASK | XAXIVDMA_IXR_ALL_MASK)

typedef struct {
	u8 reg;
	u8 val;
	u32 delay_us;
} Ov7670Reg;

static XIicPs Iic;
static XAxiVdma Vdma;
static UINTPTR FrameBuf[FRAME_COUNT];

static const Ov7670Reg Ov7670Init[] = {
	
    // linux/drivers/media/i2c/ov7670.c
    {0x12, 0x80, 20000}, // COM7: reset
	{0x11, 0x01, 1000},  // CLKRC: 30fps
    {0x3A, 0x04, 1000},  // TSLB: 神秘保留值
	{0x12, 0x00, 1000},  // COM7: VGA base
    // 画面裁剪
	// {0x17, 0x13, 1000},  // HSTART: 158
	// {0x18, 0x01, 1000},  // HSTOP: 14
	// {0x32, 0xB6, 1000},  // HREF: [2:0]HSTART [5:3]HSTOP (14 + 784 - 158) % 784 = 640
	// {0x19, 0x02, 1000},  // VSTART: 10
	// {0x1A, 0x7A, 1000},  // VSTOP: 490
	// {0x03, 0x0A, 1000},  // VREF: [1:0]VSTART [3:2]VSTOP 490 - 10 = 480
    // 色彩增益
    // {0x13, 0xE5, 1000},  // 关闭 AWC
    // {0x01, 0x76, 1000},
    // {0x02, 0x73, 1000},
    // {0x6A, 0x85, 1000},

    // fmt_RGB565
	{0x12, 0x04, 1000},  // COM7: RGB
	{0x8C, 0x00, 1000},  // RGB444 disable
	{0x40, 0x10, 1000},  // COM15: RGB565
	{0x14, 0x38, 1000},  // COM9: 16x gain + magic rsvd bit 
    // 色彩矩阵系数
	{0x4F, 0xB3, 1000},
	{0x50, 0xB3, 1000},
	{0x51, 0x00, 1000},
	{0x52, 0x3D, 1000},
	{0x53, 0xA7, 1000},
	{0x54, 0xE4, 1000},
    {0x58, 0x9E, 1000},  // 符号位 + 对比度中心
    // {0x4F, 0x40, 1000},
	// {0x50, 0x34, 1000},
	// {0x51, 0x0C, 1000},
	// {0x52, 0x17, 1000},
	// {0x53, 0x29, 1000},
	// {0x54, 0x40, 1000},
    // {0x58, 0x1E, 1000},  // 符号位 + 对比度中心
	{0x3D, 0xC0, 1000},  // COM13: gamma + UV sat

#if ENABLE_OV7670_COLORBAR
	{0x42, 0x08, 1000},
#else
	{0x42, 0x00, 1000},
#endif
};

static u32 vdma_reg(u32 offset)
{
	return XAxiVdma_ReadReg(XPAR_XAXIVDMA_0_BASEADDR, offset);
}

static void print_flag(const char *name, int set, int *printed)
{
	if (set) {
		xil_printf("%s%s", *printed ? "|" : "", name);
		*printed = 1;
	}
}

static void print_vdma_s2mm_status(u32 sr)
{
	int printed = 0;

	xil_printf("    S2MM status bits: ");
	print_flag("HALTED", (sr & XAXIVDMA_SR_HALTED_MASK) != 0U, &printed);
	print_flag("IDLE", (sr & XAXIVDMA_SR_IDLE_MASK) != 0U, &printed);
	print_flag("INTERNAL_ERR", (sr & XAXIVDMA_SR_ERR_INTERNAL_MASK) != 0U, &printed);
	print_flag("SLAVE_ERR", (sr & XAXIVDMA_SR_ERR_SLAVE_MASK) != 0U, &printed);
	print_flag("DECODE_ERR", (sr & XAXIVDMA_SR_ERR_DECODE_MASK) != 0U, &printed);
	print_flag("FSZ_LESS", (sr & XAXIVDMA_SR_ERR_FSZ_LESS_MASK) != 0U, &printed);
	print_flag("LSZ_LESS", (sr & XAXIVDMA_SR_ERR_LSZ_LESS_MASK) != 0U, &printed);
	print_flag("SG_SLV_ERR", (sr & XAXIVDMA_SR_ERR_SG_SLV_MASK) != 0U, &printed);
	print_flag("SG_DEC_ERR", (sr & XAXIVDMA_SR_ERR_SG_DEC_MASK) != 0U, &printed);
	print_flag("FSZ_MORE_SOF_LATE", (sr & XAXIVDMA_SR_ERR_FSZ_MORE_MASK) != 0U,
		   &printed);
	print_flag("FRMCNT_IRQ", (sr & XAXIVDMA_IXR_FRMCNT_MASK) != 0U, &printed);
	print_flag("DELAY_IRQ", (sr & XAXIVDMA_IXR_DELAYCNT_MASK) != 0U, &printed);
	print_flag("ERROR_IRQ", (sr & XAXIVDMA_IXR_ERROR_MASK) != 0U, &printed);

	if (!printed) {
		xil_printf("none");
	}
	xil_printf("\r\n");
}

static void clear_vdma_s2mm_latched_status(const char *tag)
{
	u32 before = vdma_reg(XAXIVDMA_RX_OFFSET + XAXIVDMA_SR_OFFSET);

	XAxiVdma_ClearDmaChannelErrors(&Vdma, XAXIVDMA_WRITE, VDMA_S2MM_W1C_MASK);

	xil_printf("[%s] cleared S2MM W1C status: before=0x%08x after=0x%08x\r\n",
		   tag, before, vdma_reg(XAXIVDMA_RX_OFFSET + XAXIVDMA_SR_OFFSET));
}

static void dump_vdma_regs(const char *tag)
{
	u32 s2mm_cr = vdma_reg(XAXIVDMA_RX_OFFSET + XAXIVDMA_CR_OFFSET);
	u32 s2mm_sr = vdma_reg(XAXIVDMA_RX_OFFSET + XAXIVDMA_SR_OFFSET);
	u32 park = vdma_reg(XAXIVDMA_PARKPTR_OFFSET);
	u32 wr_frame = (park & XAXIVDMA_PARKPTR_WRTSTR_MASK) >> 24;
	u32 stride = vdma_reg(XAXIVDMA_S2MM_ADDR_OFFSET + XAXIVDMA_STRD_FRMDLY_OFFSET);
	u32 hsize = vdma_reg(XAXIVDMA_S2MM_ADDR_OFFSET + XAXIVDMA_HSIZE_OFFSET);
	u32 vsize = vdma_reg(XAXIVDMA_S2MM_ADDR_OFFSET + XAXIVDMA_VSIZE_OFFSET);

	xil_printf("[%s] S2MM CR=0x%08x SR=0x%08x PARK=0x%08x WRT=%u STRIDE=0x%08x HSIZE=%u VSIZE=%u\r\n",
		   tag, s2mm_cr, s2mm_sr, park, wr_frame, stride, hsize, vsize);
	print_vdma_s2mm_status(s2mm_sr);
}

static int wait_i2c_idle(void)
{
	u32 timeout = 1000000U;

	while (XIicPs_BusIsBusy(&Iic) && timeout > 0U) {
		timeout--;
	}

	return timeout == 0U ? XST_FAILURE : XST_SUCCESS;
}

static int i2c_init(void)
{
	XIicPs_Config *config;
	int status;

	config = XIicPs_LookupConfig(XPAR_XIICPS_0_BASEADDR);
	if (config == NULL) {
		xil_printf("I2C lookup failed\r\n");
		return XST_FAILURE;
	}

	status = XIicPs_CfgInitialize(&Iic, config, config->BaseAddress);
	if (status != XST_SUCCESS) {
		xil_printf("I2C initialize failed: %d\r\n", status);
		return status;
	}

	status = XIicPs_SetSClk(&Iic, I2C_CLK_RATE_HZ);
	if (status != XST_SUCCESS) {
		xil_printf("I2C set clock failed: %d\r\n", status);
		return status;
	}

	xil_printf("I2C0 ready, SCLK=%u Hz, OV7670 7-bit addr=0x%02x\r\n",
		   I2C_CLK_RATE_HZ, OV7670_SCCB_ADDR);
	return XST_SUCCESS;
}

static int ov7670_write_reg(u8 reg, u8 val)
{
	u8 tx[2] = {reg, val};
	int status;

	status = wait_i2c_idle();
	if (status != XST_SUCCESS) {
		xil_printf("I2C busy before write reg 0x%02x\r\n", reg);
		return status;
	}

	status = XIicPs_MasterSendPolled(&Iic, tx, sizeof(tx), OV7670_SCCB_ADDR);
	if (status != XST_SUCCESS) {
		xil_printf("OV7670 write failed: reg=0x%02x val=0x%02x status=%d\r\n",
			   reg, val, status);
		return status;
	}

	return wait_i2c_idle();
}

static int ov7670_read_reg(u8 reg, u8 *val)
{
	int status;

	status = wait_i2c_idle();
	if (status != XST_SUCCESS) {
		xil_printf("I2C busy before read reg 0x%02x\r\n", reg);
		return status;
	}

	status = XIicPs_MasterSendPolled(&Iic, &reg, 1, OV7670_SCCB_ADDR);
	if (status != XST_SUCCESS) {
		xil_printf("OV7670 read address phase failed: reg=0x%02x status=%d\r\n",
			   reg, status);
		return status;
	}

	status = wait_i2c_idle();
	if (status != XST_SUCCESS) {
		xil_printf("I2C busy after read address phase\r\n");
		return status;
	}

	status = XIicPs_MasterRecvPolled(&Iic, val, 1, OV7670_SCCB_ADDR);
	if (status != XST_SUCCESS) {
		xil_printf("OV7670 read data phase failed: reg=0x%02x status=%d\r\n",
			   reg, status);
		return status;
	}

	return wait_i2c_idle();
}

static int ov7670_probe(void)
{
	u8 pid = 0;
	u8 ver = 0;
	int status;

	status = ov7670_read_reg(0x0a, &pid);
	if (status != XST_SUCCESS) {
		return status;
	}

	status = ov7670_read_reg(0x0b, &ver);
	if (status != XST_SUCCESS) {
		return status;
	}

	xil_printf("OV7670 ID: PID=0x%02x VER=0x%02x\r\n", pid, ver);

	return XST_SUCCESS;
}

static int ov7670_init(void)
{
	unsigned int i;
	int status;

	status = ov7670_probe();
	if (status != XST_SUCCESS) {
		xil_printf("OV7670 ID read failed\r\n");
	}

	for (i = 0; i < (sizeof(Ov7670Init) / sizeof(Ov7670Init[0])); i++) {
		status = ov7670_write_reg(Ov7670Init[i].reg, Ov7670Init[i].val);
		if (status != XST_SUCCESS) {
			return status;
		}
		if (Ov7670Init[i].delay_us != 0U) {
			usleep(Ov7670Init[i].delay_us);
		}
	}

	status = ov7670_probe();
	if (status != XST_SUCCESS) {
		xil_printf("WARNING: OV7670 ID read failed after init\r\n");
	}

	
	return XST_SUCCESS;
}

static void frame_buffer_init(void)
{
	u32 i;

	for (i = 0U; i < FRAME_COUNT; i++) {
		FrameBuf[i] = (UINTPTR)(FRAME_BUF_BASE + (i * FRAME_BYTES));
	}

	for (i = 0U; i < FRAME_BUF_TOTAL_BYTES; i += 0x100000U) {
		Xil_SetTlbAttributes((INTPTR)(FRAME_BUF_BASE + i), NORM_NONCACHE);
	}

	for (i = 0U; i < (FRAME_BUF_TOTAL_BYTES / sizeof(u32)); i++) {
		Xil_Out32(FRAME_BUF_BASE + (i * sizeof(u32)), FRAME_BUF_FILL_WORD);
	}

	Xil_DCacheFlushRange((INTPTR)FRAME_BUF_BASE, FRAME_BUF_TOTAL_BYTES);
	xil_printf("Frame buffers: base=0x%08x bytes/frame=%u total=%u\r\n",
		   FRAME_BUF_BASE, FRAME_BYTES, FRAME_BUF_TOTAL_BYTES);
}

static int vdma_init(void)
{
	XAxiVdma_Config *config;
	XAxiVdma_DmaSetup write_cfg;
	int status;
	unsigned int i;

	config = XAxiVdma_LookupConfig(XPAR_XAXIVDMA_0_BASEADDR);
	if (config == NULL) {
		xil_printf("VDMA lookup failed at base 0x%08x\r\n", XPAR_XAXIVDMA_0_BASEADDR);
		return XST_FAILURE;
	}

	status = XAxiVdma_CfgInitialize(&Vdma, config, config->BaseAddress);
	if (status != XST_SUCCESS) {
		xil_printf("VDMA initialize failed: %d\r\n", status);
		return status;
	}

	XAxiVdma_Reset(&Vdma, XAXIVDMA_WRITE);
	while (XAxiVdma_ResetNotDone(&Vdma, XAXIVDMA_WRITE)) {
	}

	XAxiVdma_IntrDisable(&Vdma, XAXIVDMA_IXR_ALL_MASK, XAXIVDMA_WRITE);
	clear_vdma_s2mm_latched_status("after-reset");

	status = XAxiVdma_FsyncSrcSelect(&Vdma, XAXIVDMA_S2MM_TUSER_FSYNC,
					 XAXIVDMA_WRITE);
	if (status != XST_SUCCESS) {
		xil_printf("VDMA S2MM TUSER fsync select failed: %d\r\n", status);
		return status;
	}

	memset(&write_cfg, 0, sizeof(write_cfg));
	write_cfg.VertSizeInput = FRAME_HEIGHT;
	write_cfg.HoriSizeInput = FRAME_STRIDE_BYTES;
	write_cfg.Stride = FRAME_STRIDE_BYTES;  // 无 padding
	write_cfg.FrameDelay = 0;
	write_cfg.EnableCircularBuf = 1;
	write_cfg.EnableSync = 0;
	write_cfg.PointNum = 0;
	write_cfg.EnableFrameCounter = 0;
	write_cfg.FixedFrameStoreAddr = 0;
	write_cfg.GenLockRepeat = 1;
	write_cfg.EnableVFlip = 0;

	for (i = 0U; i < FRAME_COUNT; i++) {
		write_cfg.FrameStoreStartAddr[i] = FrameBuf[i];
	}

	status = XAxiVdma_DmaConfig(&Vdma, XAXIVDMA_WRITE, &write_cfg);
	if (status != XST_SUCCESS) {
		xil_printf("VDMA S2MM config failed: %d\r\n", status);
		return status;
	}

	status = XAxiVdma_DmaSetBufferAddr(&Vdma, XAXIVDMA_WRITE,
					   write_cfg.FrameStoreStartAddr);
	if (status != XST_SUCCESS) {
		xil_printf("VDMA S2MM buffer address setup failed: %d\r\n", status);
		return status;
	}

	dump_vdma_regs("before-start");

	status = XAxiVdma_DmaStart(&Vdma, XAXIVDMA_WRITE);
	if (status != XST_SUCCESS) {
		xil_printf("VDMA S2MM start failed: %d\r\n", status);
		return status;
	}

	dump_vdma_regs("after-start");
	usleep(VDMA_SETTLE_US);
	clear_vdma_s2mm_latched_status("after-settle");
	dump_vdma_regs("after-settle-clear");
	return XST_SUCCESS;
}

static int vdma_check_status(void)
{
	u32 sr = vdma_reg(XAXIVDMA_RX_OFFSET + XAXIVDMA_SR_OFFSET);
	int errors = XAxiVdma_GetDmaChannelErrors(&Vdma, XAXIVDMA_WRITE);

	if ((sr & XAXIVDMA_SR_ERR_ALL_MASK) != 0U || errors != 0) {
		xil_printf("VDMA S2MM error: SR=0x%08x driver_errors=0x%08x\r\n",
			   sr, errors);
		print_vdma_s2mm_status(sr);
		return XST_FAILURE;
	}

	return XST_SUCCESS;
}

static void print_frame_samples(UINTPTR frame_addr)
{
	u32 x;

	Xil_DCacheInvalidateRange((INTPTR)frame_addr, FRAME_BYTES);
	xil_printf("\r\nNEW FRAME\r\n");

	for (u32 y = 0U; y < FRAME_HEIGHT; y += 16) {

		xil_printf("line %u:", y);
		for (x = 0U; x < FRAME_WIDTH; x += 16) {
			u32 pixel = Xil_In32(frame_addr + ((y * FRAME_WIDTH + x) * sizeof(u32)));
			xil_printf(" %08x", pixel);
		}
		xil_printf("\r\n");
	}
}

int main(void)
{
	int status;
	u32 ptr;

	xil_printf("\r\nExpect: %u*%u\r\n", FRAME_WIDTH, FRAME_HEIGHT);

	frame_buffer_init();
    usleep(20000);

	status = i2c_init();
	if (status != XST_SUCCESS) {
		xil_printf("FAIL: I2C init\r\n");
		return XST_FAILURE;
	}
    usleep(20000);

	status = ov7670_init();
	if (status != XST_SUCCESS) {
		xil_printf("FAIL: OV7670 init\r\n");
		return XST_FAILURE;
	}

	status = vdma_init();
	if (status != XST_SUCCESS) {
		xil_printf("FAIL: VDMA init\r\n");
		return XST_FAILURE;
	}

	xil_printf("Start capturing.\r\n");
	while (1) {
        u32 park = vdma_reg(XAXIVDMA_PARKPTR_OFFSET);
	    u32 wr_frame = (park & XAXIVDMA_PARKPTR_WRTSTR_MASK) >> 24;
        ptr = (wr_frame == 0) ? 2 : ((wr_frame == 1) ? 0 : 1);

		sleep(1);
		dump_vdma_regs("poll");
		(void)vdma_check_status();

		print_frame_samples(FrameBuf[ptr]);
	}

	return XST_SUCCESS;
}
