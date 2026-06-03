/******************************************************************************
* Copyright (C) 2023 Advanced Micro Devices, Inc. All Rights Reserved.
* SPDX-License-Identifier: MIT
******************************************************************************/
/*
 * helloworld.c: simple test application
 *
 * This application configures UART 16550 to baud rate 9600.
 * PS7 UART (Zynq) is not initialized by this application, since
 * bootrom/bsp configures it to baud rate 115200
 *
 * ------------------------------------------------
 * | UART TYPE   BAUD RATE                        |
 * ------------------------------------------------
 *   uartns550   9600
 *   uartlite    Configurable only in HW design
 *   ps7_uart    115200 (configured by bootrom/bsp)
 */

#include <stdio.h>
#include "platform.h"
#include "xil_printf.h"
#include "myOLED.h"
#include "xparameters.h"
#include "xil_io.h"
#include "sleep.h"

// reg0 : start
// reg1 : data
// reg2 : dc
// reg3 : busy

// // 发送一个字节命令/数据
// void spi_send_byte(u8 data, u8 dc){

//     while(MYOLED_mReadReg(XPAR_MYOLED_0_BASEADDR, MYOLED_S00_AXI_SLV_REG3_OFFSET));

//     MYOLED_mWriteReg(XPAR_MYOLED_0_BASEADDR, MYOLED_S00_AXI_SLV_REG2_OFFSET, dc?1:0);

//     MYOLED_mWriteReg(XPAR_MYOLED_0_BASEADDR, MYOLED_S00_AXI_SLV_REG1_OFFSET, data);

//     MYOLED_mWriteReg(XPAR_MYOLED_0_BASEADDR, MYOLED_S00_AXI_SLV_REG0_OFFSET, 1);
//     usleep(1);
//     MYOLED_mWriteReg(XPAR_MYOLED_0_BASEADDR, MYOLED_S00_AXI_SLV_REG0_OFFSET, 0);
// }

int main() {
    // u8 data1=0x00, data2=0x80;
    // init_platform();

    // usleep(500000); 

    // while(1){
    //     spi_send_byte(data1, 0);
    //     if(data1 < 0x7F) data1++;
    //     else data1 = 0x00;

    //     spi_send_byte(data2, 1);
    //     if(data2 < 0xFF) data2++;
    //     else data2 = 0x80;
    // }

    init_platform();
    usleep(500000);

    oled_init();
    oled_clear();
    oled_refresh();

    oled_set_size(FONT_6X8);
    oled_show_string(0, 0, "Font 6x8");

    oled_set_size(FONT_8X16);
    oled_show_string(0, 10, "Font 8x16");

    oled_set_size(FONT_12X24);
    oled_show_string(0, 40, "Font 12x24");

    oled_refresh();
    while (1);
    return 0;
}
