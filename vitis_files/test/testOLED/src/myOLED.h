
#ifndef MYOLED_H
#define MYOLED_H


/****************** Include Files ********************/
#include "xil_types.h"
#include "xstatus.h"

typedef enum {
    FONT_6X8   = 0,
    FONT_8X16  = 1,
    FONT_12X24 = 2
} OLED_FontSize;

#define MYOLED_S00_AXI_SLV_REG0_OFFSET 0
#define MYOLED_S00_AXI_SLV_REG1_OFFSET 4
#define MYOLED_S00_AXI_SLV_REG2_OFFSET 8
#define MYOLED_S00_AXI_SLV_REG3_OFFSET 12

// OLED 参数
#define OLED_WIDTH  128
#define OLED_HEIGHT 64
#define OLED_PAGES  (OLED_HEIGHT / 8)   // 8 页

// 基本命令（部分）
#define OLED_CMD_DISPLAY_OFF    0xAE
#define OLED_CMD_DISPLAY_ON     0xAF
#define OLED_CMD_SET_CONTRAST   0x81
#define OLED_CMD_SET_MUX_RATIO  0xA8
#define OLED_CMD_SET_DISPLAY_OFFSET 0xD3
#define OLED_CMD_SET_START_LINE 0x40
#define OLED_CMD_CHARGE_PUMP    0x8D
#define OLED_CMD_SET_MEM_ADDR_MODE 0x20
#define OLED_CMD_SET_COL_ADDR   0x21
#define OLED_CMD_SET_PAGE_ADDR  0x22
#define OLED_CMD_SET_SEG_REMAP  0xA0
#define OLED_CMD_SET_COM_SCAN_DIR 0xC0
#define OLED_CMD_SET_COM_PINS   0xDA
#define OLED_CMD_SET_DISPLAY_CLOCK_DIV 0xD5
#define OLED_CMD_SET_PRECHARGE  0xD9
#define OLED_CMD_SET_VCOM_DESELECT 0xDB
#define OLED_CMD_NORMAL_DISPLAY 0xA6
#define OLED_CMD_INVERT_DISPLAY 0xA7

// 函数声明
void oled_init(void);
void oled_write_cmd(u8 cmd);
void oled_write_data(u8 data);
void oled_set_pos(u8 page, u8 col);
void oled_clear(void);
void oled_fill(u8 pattern);
void oled_set_pixel(u8 x, u8 y, u8 color);
void oled_show_char(u8 x, u8 y, char ch);
void oled_show_string(u8 x, u8 y, const char *str);
void oled_refresh(void);  // 从显存刷新全屏
void oled_set_size(OLED_FontSize size);

/**************************** Type Definitions *****************************/
/**
 *
 * Write a value to a MYOLED register. A 32 bit write is performed.
 * If the component is implemented in a smaller width, only the least
 * significant data is written.
 *
 * @param   BaseAddress is the base address of the MYOLEDdevice.
 * @param   RegOffset is the register offset from the base to write to.
 * @param   Data is the data written to the register.
 *
 * @return  None.
 *
 * @note
 * C-style signature:
 * 	void MYOLED_mWriteReg(u32 BaseAddress, unsigned RegOffset, u32 Data)
 *
 */
#define MYOLED_mWriteReg(BaseAddress, RegOffset, Data) \
  	Xil_Out32((BaseAddress) + (RegOffset), (u32)(Data))

/**
 *
 * Read a value from a MYOLED register. A 32 bit read is performed.
 * If the component is implemented in a smaller width, only the least
 * significant data is read from the register. The most significant data
 * will be read as 0.
 *
 * @param   BaseAddress is the base address of the MYOLED device.
 * @param   RegOffset is the register offset from the base to write to.
 *
 * @return  Data is the data from the register.
 *
 * @note
 * C-style signature:
 * 	u32 MYOLED_mReadReg(u32 BaseAddress, unsigned RegOffset)
 *
 */
#define MYOLED_mReadReg(BaseAddress, RegOffset) \
    Xil_In32((BaseAddress) + (RegOffset))

/************************** Function Prototypes ****************************/
/**
 *
 * Run a self-test on the driver/device. Note this may be a destructive test if
 * resets of the device are performed.
 *
 * If the hardware system is not built correctly, this function may never
 * return to the caller.
 *
 * @param   baseaddr_p is the base address of the MYOLED instance to be worked on.
 *
 * @return
 *
 *    - XST_SUCCESS   if all self-test code passed
 *    - XST_FAILURE   if any self-test code failed
 *
 * @note    Caching must be turned off for this function to work.
 * @note    Self test may fail if data memory and device are not on the same bus.
 *
 */
XStatus MYOLED_Reg_SelfTest(void * baseaddr_p);

#endif // MYOLED_H
