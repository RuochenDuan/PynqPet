#include <stdio.h>
#include "xil_printf.h"
#include "xil_io.h"
#include "xparameters.h"
#include "sleep.h"

#define MYI2C_DS3231_BASEADDR XPAR_MYI2C_DS3231_0_BASEADDR



// AXI 寄存器偏移

#define RTC_CTRL_OFFSET        0x00
#define RTC_SET_TIME1_OFFSET   0x04
#define RTC_SET_TIME2_OFFSET   0x08
#define RTC_STATUS_OFFSET      0x0C
#define RTC_READ_TIME1_OFFSET  0x10
#define RTC_READ_TIME2_OFFSET  0x14
#define RTC_DEBUG1_OFFSET      0x18
#define RTC_DEBUG2_OFFSET      0x1C

// 控制位

#define RTC_CTRL_START_READ    0x00000001
#define RTC_CTRL_START_SET     0x00000002

//状态位


#define RTC_STATUS_BUSY        0x00000001
#define RTC_STATUS_DONE        0x00000002
#define RTC_STATUS_ACK_ERROR   0x00000004


// 寄存器读写函数


void rtc_write(u32 offset, u32 value)
{
    Xil_Out32(MYI2C_DS3231_BASEADDR + offset, value);
}

u32 rtc_read(u32 offset)
{
    return Xil_In32(MYI2C_DS3231_BASEADDR + offset);
}

// 等待 done
int rtc_wait_done(void)
{
    u32 status;
    int timeout = 1000000;

    while (timeout > 0) {
        status = rtc_read(RTC_STATUS_OFFSET);

        if (status & RTC_STATUS_DONE) {
            if (status & RTC_STATUS_ACK_ERROR) {
                xil_printf("RTC ERROR: ACK error. status = 0x%08lx\r\n", status);
                return -1;
            }

            return 0;
        }

        timeout--;
    }

    xil_printf("RTC ERROR: timeout. status = 0x%08lx\r\n", rtc_read(RTC_STATUS_OFFSET));
    xil_printf("DEBUG1 = 0x%08lx\r\n", rtc_read(RTC_DEBUG1_OFFSET));
    xil_printf("DEBUG2 = 0x%08lx\r\n", rtc_read(RTC_DEBUG2_OFFSET));

    return -2;
}

// 触发读时间
int rtc_start_read(void)
{
    // 先清 0，再写 1，制造上升沿
    rtc_write(RTC_CTRL_OFFSET, 0x00000000);
    usleep(10);

    rtc_write(RTC_CTRL_OFFSET, RTC_CTRL_START_READ);
    usleep(10);

    rtc_write(RTC_CTRL_OFFSET, 0x00000000);

    return rtc_wait_done();
}

// 触发设置时间
// 输入都是普通十进制数，不是 BCD
int rtc_set_time(
    u8 sec,
    u8 min,
    u8 hour,
    u8 week,
    u8 date,
    u8 month,
    u8 year
)
{
    u32 set_time1;
    u32 set_time2;

    // slv_reg1:
    // [7:0]   sec
    // [15:8]  min
    // [23:16] hour
    // [31:24] week
    set_time1 = ((u32)week << 24) |
                ((u32)hour << 16) |
                ((u32)min  << 8)  |
                ((u32)sec);

    // slv_reg2:
    // [7:0]   date
    // [15:8]  month
    // [23:16] year
    set_time2 = ((u32)year  << 16) |
                ((u32)month << 8)  |
                ((u32)date);

    rtc_write(RTC_SET_TIME1_OFFSET, set_time1);
    rtc_write(RTC_SET_TIME2_OFFSET, set_time2);

    // set_start 上升沿
    rtc_write(RTC_CTRL_OFFSET, 0x00000000);
    usleep(10);

    rtc_write(RTC_CTRL_OFFSET, RTC_CTRL_START_SET);
    usleep(10);

    rtc_write(RTC_CTRL_OFFSET, 0x00000000);

    return rtc_wait_done();
}

// 读回来的时间

void rtc_print_time(void)
{
    u32 time1;
    u32 time2;

    u8 sec;
    u8 min;
    u8 hour;
    u8 week;
    u8 date;
    u8 month;
    u8 year;

    time1 = rtc_read(RTC_READ_TIME1_OFFSET);
    time2 = rtc_read(RTC_READ_TIME2_OFFSET);

    sec   = (time1 >> 0)  & 0xFF; //8位
    min   = (time1 >> 8)  & 0xFF;
    hour  = (time1 >> 16) & 0xFF;
    week  = (time1 >> 24) & 0xFF;

    date  = (time2 >> 0)  & 0xFF;
    month = (time2 >> 8)  & 0xFF;
    year  = (time2 >> 16) & 0xFF;

    xil_printf("RTC TIME: 20%02d-%02d-%02d week=%d %02d:%02d:%02d\r\n",
               year, month, date, week, hour, min, sec);
}

int main()
{
    int ret;

    xil_printf("\r\n");
    xil_printf(" DS3231 RTC AXI Test Start\r\n");


    // 设置时间
    // 例如设置为 2026-05-13 星期三 15:30:00
    // year = 26，不是 2026
    xil_printf("Set RTC time...\r\n");

    ret = rtc_set_time(
        0,    // sec
        52,   // min
        13,   // hour
        3,    // week
        20,   // date
        5,    // month
        26    // year
    );

    if (ret != 0) {
        xil_printf("Set RTC time failed.\r\n");
    } else {
        xil_printf("Set RTC time done.\r\n");
    }

    sleep(1);

    // 循环读取时间
    while (1) {
        ret = rtc_start_read();

        if (ret == 0) {
            rtc_print_time();
        } else {
            xil_printf("Read RTC failed.\r\n");
        }

        sleep(1);
    }

    return 0;
}