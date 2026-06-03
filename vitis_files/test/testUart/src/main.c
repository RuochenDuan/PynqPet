#include "xil_io.h"
#include "xil_printf.h"
#include "sleep.h"

#define UART_BASE        0x43C50000U
#define REG_TX_DATA      0x00U
#define REG_RX_DATA      0x04U
#define REG_STATUS       0x08U
#define REG_CTRL         0x0CU
#define REG_FIFO_CNT     0x10U

#define CTRL_TX_EN       0x00000001U
#define CTRL_RX_EN       0x00000002U
#define STATUS_TX_EMPTY  0x00000001U
#define STATUS_RX_EMPTY  0x00000002U
#define STATUS_TX_FULL   0x00000004U

static unsigned int fifo_count_reg(void)
{
    return Xil_In32(UART_BASE + REG_FIFO_CNT);
}

static unsigned int tx_fifo_count(unsigned int fifo_count)
{
    return (fifo_count >> 8) & 0x1FU;
}

static unsigned int rx_fifo_count(unsigned int fifo_count)
{
    return fifo_count & 0x1FU;
}

int main(void)
{
    unsigned int i;
    const unsigned char test_bytes[] = {0x55, 0xA5, 0x0D, 0x0A};

    xil_printf("uart ps axi check start\r\n");
    xil_printf("For loopback test, connect uart_tx pin to uart_rx pin.\r\n");

    Xil_Out32(UART_BASE + REG_CTRL, CTRL_TX_EN | CTRL_RX_EN);

    for (i = 0; i < sizeof(test_bytes); i++) {
        unsigned int fifo_count;

        while (Xil_In32(UART_BASE + REG_STATUS) & STATUS_TX_FULL) {
        }
        Xil_Out32(UART_BASE + REG_TX_DATA, test_bytes[i]);
        fifo_count = fifo_count_reg();
        xil_printf("tx 0x%02x status=0x%08x fifo=0x%08x tx_count=%u rx_count=%u\r\n",
                   test_bytes[i],
                   Xil_In32(UART_BASE + REG_STATUS),
                   fifo_count,
                   tx_fifo_count(fifo_count),
                   rx_fifo_count(fifo_count));
    }

    for (i = 0; i < 2000000U; i++) {
        unsigned int status = Xil_In32(UART_BASE + REG_STATUS);
        if ((status & STATUS_RX_EMPTY) == 0U) {
            unsigned int data = Xil_In32(UART_BASE + REG_RX_DATA) & 0xFFU;
            unsigned int fifo_count = fifo_count_reg();

            xil_printf("rx 0x%02x status=0x%08x fifo=0x%08x tx_count=%u rx_count=%u\r\n",
                       data,
                       Xil_In32(UART_BASE + REG_STATUS),
                       fifo_count,
                       tx_fifo_count(fifo_count),
                       rx_fifo_count(fifo_count));
        }
    }

    while ((Xil_In32(UART_BASE + REG_STATUS) & STATUS_TX_EMPTY) == 0U) {
    }

    xil_printf("uart ps axi check done\r\n");
    while (1) {
        sleep(1);
    }
}
