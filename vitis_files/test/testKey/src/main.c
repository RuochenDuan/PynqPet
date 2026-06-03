#include "xil_io.h"
#include "xil_printf.h"
#include "sleep.h"

#define KEYPAD_BASE      0x43C30000U
#define REG_KEY_DATA     0x00U
#define REG_KEY_STATUS   0x04U
#define REG_SCAN_CTRL    0x08U
#define REG_KEY_MAP      0x0CU

#define SCAN_EN          0x00000001U

static const char *event_name(unsigned int event)
{
    switch (event) {
    case 1: return "short";
    case 2: return "long";
    default: return "none";
    }
}

static unsigned int keypad_to_panel_code(unsigned int raw_code)
{
    return ((raw_code & 0x3U) << 2) | ((raw_code >> 2) & 0x3U);
}

static char keypad_label(unsigned int code)
{
    static const char labels[16] = {
        '1', '2', '3', 'A',
        '4', '5', '6', 'B',
        '7', '8', '9', 'C',
        '*', '0', '#', 'D'
    };
    return labels[code & 0xFU];
}

int main(void)
{
    xil_printf("keypad ps axi check start\r\n");
    Xil_Out32(KEYPAD_BASE + REG_SCAN_CTRL, SCAN_EN);

    while (1) {
        unsigned int status = Xil_In32(KEYPAD_BASE + REG_KEY_STATUS);
        unsigned int key_map = Xil_In32(KEYPAD_BASE + REG_KEY_MAP);

        if (status & 0x1U) {
            unsigned int data = Xil_In32(KEYPAD_BASE + REG_KEY_DATA);
            unsigned int event = (data >> 6) & 0x3U;
            unsigned int raw_code = data & 0xFU;
            unsigned int code = keypad_to_panel_code(raw_code);

            xil_printf("event=%s key=%c code=%u raw_code=%u key_map=0x%04x raw=0x%08x\r\n",
                       event_name(event), keypad_label(code), code, raw_code, key_map, data);
        }

        usleep(5000);
    }
}
