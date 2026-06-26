/*
 * ingenic_flash — compact C flasher for Ingenic T33ZN (T-series) over USB boot.
 *
 * A faithful, dependency-light port of the `detect` and `flash` (SPI NOR)
 * paths of ingenic-flash-tool. The board firmware (ginfo/spl/uboot) and the
 * cfg1/cfg2 payloads — already patched for the board's NOR chip — are embedded
 * in firmware_blob.h (regenerate with gen_blob.py). Only dependency: libusb-1.0.
 *
 *   ingenic_flash detect
 *   ingenic_flash flash <image> [--offset N] [--erase-all] [--no-reboot]
 *                       [--gpio PORT STATE]...
 */
#include <ctype.h>
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <time.h>
#include <unistd.h>

#include <libusb-1.0/libusb.h>

#include "firmware_blob.h"

/* Boot ROM vendor requests */
#define VR_GET_CPU_INFO     0x00
#define VR_SET_DATA_ADDRESS 0x01
#define VR_SET_DATA_LENGTH  0x02
#define VR_FLUSH_CACHES     0x03
#define VR_PROGRAM_START1   0x04
#define VR_PROGRAM_START2   0x05
/* Stage2 (burner) vendor requests */
#define VR_GET_ACK          0x10
#define VR_INIT             0x11
#define VR_WRITE            0x12
#define VR_UPDATE_CFG       0x14
#define VR_REBOOT           0x16
#define VR_GET_FLASH_INFO   0x26

#define REQ_OUT 0x40
#define REQ_IN  0xC0
#define EP_OUT  0x01
#define EP_IN   0x81

#define CHUNK        65536
#define CTRL_TIMEOUT 20000   /* ms */
#define BULK_TIMEOUT 120000  /* ms */
#define BOOT_WAIT_MS 10000

/* Ingenic GPIO controller, uncached KSEG1 view of physical 0x10010000.
 * Ports are 0x1000 apart (PA..PD). Register offsets match the firmware's
 * board_early_init_f() sequence (write-1-to-act set/clear pairs). */
#define GPIO_BASE 0xB0010000u

static libusb_context *ctx;
static libusb_device_handle *dev;

struct gpio_write { uint32_t port_offset; int pin; int on; };
static struct gpio_write gpios[16];
static int n_gpios;

/* ---- error helpers ------------------------------------------------------ */
static void die(const char *msg)
{
	fprintf(stderr, "Error: %s\n", msg);
	if (dev) libusb_close(dev);
	if (ctx) libusb_exit(ctx);
	exit(1);
}

static void die_usb(const char *msg, int rc)
{
	fprintf(stderr, "Error: %s: %s\n", msg, libusb_error_name(rc));
	if (dev) libusb_close(dev);
	if (ctx) libusb_exit(ctx);
	exit(1);
}

/* ---- low-level transfers ------------------------------------------------ */
static void ctrl_out(uint8_t req, uint16_t wval, uint16_t widx,
		     const unsigned char *data, uint16_t len)
{
	int rc = libusb_control_transfer(dev, REQ_OUT, req, wval, widx,
					 (unsigned char *)data, len, CTRL_TIMEOUT);
	if (rc < 0) die_usb("control OUT failed", rc);
}

static int ctrl_in(uint8_t req, unsigned char *buf, uint16_t len)
{
	int rc = libusb_control_transfer(dev, REQ_IN, req, 0, 0, buf, len, CTRL_TIMEOUT);
	return rc; /* caller decides; negative = libusb error */
}

static void bulk_out(const unsigned char *data, size_t len)
{
	size_t off = 0;
	int timeout = BULK_TIMEOUT + (int)(len / CHUNK) * 1000;
	while (off < len) {
		int n = (int)((len - off > CHUNK) ? CHUNK : (len - off));
		int sent = 0;
		int rc = libusb_bulk_transfer(dev, EP_OUT,
					      (unsigned char *)data + off, n,
					      &sent, timeout);
		if (rc < 0) die_usb("bulk OUT failed", rc);
		if (sent <= 0) die("bulk OUT wrote 0 bytes");
		off += (size_t)sent;
	}
}

/* split a 32-bit value into (wValue=high16, wIndex=low16) */
static void set_addr(uint32_t a) { ctrl_out(VR_SET_DATA_ADDRESS, a >> 16, a & 0xFFFF, NULL, 0); }
static void set_len(uint32_t l)  { ctrl_out(VR_SET_DATA_LENGTH,  l >> 16, l & 0xFFFF, NULL, 0); }

static int get_cpu_info(unsigned char buf[8]) { return ctrl_in(VR_GET_CPU_INFO, buf, 8); }

static int32_t get_ack(int timeout_ms)
{
	unsigned char b[4];
	int rc = libusb_control_transfer(dev, REQ_IN, VR_GET_ACK, 0, 0, b, 4, timeout_ms);
	if (rc != 4) return INT32_MIN; /* signal "no valid ack" */
	return (int32_t)((uint32_t)b[0] | b[1] << 8 | b[2] << 16 | (uint32_t)b[3] << 24);
}

static void put_le32(unsigned char *p, uint32_t v)
{
	p[0] = v & 0xFF; p[1] = (v >> 8) & 0xFF;
	p[2] = (v >> 16) & 0xFF; p[3] = (v >> 24) & 0xFF;
}

static void upload(uint32_t addr, const unsigned char *data, size_t len)
{
	set_addr(addr);
	set_len((uint32_t)len);
	bulk_out(data, len);
}

/* Write one 32-bit word to an arbitrary address via the boot ROM's download
 * primitive. Only valid in boot-ROM mode, before SPL load. */
static void poke32(uint32_t addr, uint32_t val)
{
	unsigned char b[4];
	put_le32(b, val);
	set_addr(addr);
	set_len(4);
	bulk_out(b, 4);
}

/* Drive requested GPIOs as output high/low via the boot ROM, before any SPL —
 * the earliest reachable point (e.g. to assert a PMIC power-hold line so the
 * board stays alive for the whole flash). Mirrors protocol.apply_gpio_writes. */
static void apply_gpios(int verbose)
{
	for (int i = 0; i < n_gpios; i++) {
		uint32_t base = GPIO_BASE + gpios[i].port_offset;
		uint32_t bit = 1u << gpios[i].pin;
		if (verbose)
			fprintf(stderr, "GPIO P%c%d -> %s\n",
				'A' + (int)(gpios[i].port_offset / 0x1000),
				gpios[i].pin, gpios[i].on ? "on" : "off");
		poke32(base + 0x18, bit);    /* PXINTC  - GPIO mode */
		poke32(base + 0x24, bit);    /* PXMSKS  - GPIO active */
		poke32(base + 0x38, bit);    /* PXPAT1C - output mode */
		poke32(base + 0x118, bit);   /* PXPUENC - pull-up off */
		if (gpios[i].on) {
			poke32(base + 0x44, bit);    /* PXPAT0S - drive high */
			poke32(base + 0x128, bit);   /* PXPDENC - pull-down off */
		} else {
			poke32(base + 0x48, bit);    /* PXPAT0C - drive low */
			poke32(base + 0x124, bit);   /* PXPDENS - pull-down on */
		}
	}
}

/* Parse "--gpio PORT STATE": PORT like PB30/B30, STATE on/off/high/low/1/0. */
static int parse_gpio(const char *port, const char *state)
{
	if (n_gpios >= (int)(sizeof(gpios) / sizeof(gpios[0]))) return -1;
	const char *p = port;
	if (*p == 'P' || *p == 'p') p++;
	if (!isalpha((unsigned char)*p)) return -1;
	char letter = (char)toupper((unsigned char)*p++);
	if (letter < 'A' || letter > 'D') return -1;
	char *end;
	long pin = strtol(p, &end, 10);
	if (*p == '\0' || *end != '\0' || pin < 0 || pin > 31) return -1;

	int on;
	if (!strcasecmp(state, "on") || !strcasecmp(state, "high") || !strcmp(state, "1"))
		on = 1;
	else if (!strcasecmp(state, "off") || !strcasecmp(state, "low") || !strcmp(state, "0"))
		on = 0;
	else
		return -1;

	gpios[n_gpios].port_offset = (uint32_t)(letter - 'A') * 0x1000;
	gpios[n_gpios].pin = (int)pin;
	gpios[n_gpios].on = on;
	n_gpios++;
	return 0;
}

/* ---- CRC32 (zlib polynomial), then bitwise-inverted as the burner wants -- */
static uint32_t crc32_inv(const unsigned char *p, size_t n)
{
	static uint32_t tab[256];
	static int init = 0;
	if (!init) {
		for (uint32_t i = 0; i < 256; i++) {
			uint32_t c = i;
			for (int k = 0; k < 8; k++)
				c = (c & 1) ? 0xEDB88320u ^ (c >> 1) : c >> 1;
			tab[i] = c;
		}
		init = 1;
	}
	uint32_t crc = 0xFFFFFFFFu;
	for (size_t i = 0; i < n; i++)
		crc = tab[(crc ^ p[i]) & 0xFF] ^ (crc >> 8);
	crc ^= 0xFFFFFFFFu;          /* standard zlib crc32 */
	return ~crc & 0xFFFFFFFFu;   /* match Python: ~crc32(chunk) */
}

/* ---- device open -------------------------------------------------------- */
static int open_device(void)
{
	dev = libusb_open_device_with_vid_pid(ctx, INGENIC_VID, BOOT_PID);
	if (!dev) return 0;
	libusb_set_auto_detach_kernel_driver(dev, 1);
	libusb_claim_interface(dev, 0); /* best-effort; boot ROM has one iface */
	return 1;
}

/* Poll for the device to appear in USB boot mode, up to wait_s seconds.
 * Mirrors the Python tool's find_device(wait=...). Returns 1 once open. */
static int open_device_wait(double wait_s)
{
	if (open_device()) return 1;
	if (wait_s <= 0) return 0;
	fprintf(stderr, "Waiting for device...\n");
	struct timespec half = { 0, 500 * 1000 * 1000 }; /* 500 ms */
	for (double elapsed = 0; elapsed < wait_s; elapsed += 0.5) {
		nanosleep(&half, NULL);
		if (open_device()) return 1;
	}
	return 0;
}

static void wait_cpu_info(int timeout_ms, const char *what)
{
	unsigned char info[8];
	struct timespec ts = { 0, 100 * 1000 * 1000 }; /* 100 ms */
	for (int waited = 0; waited <= timeout_ms; waited += 100) {
		if (get_cpu_info(info) == 8) return;
		nanosleep(&ts, NULL);
	}
	fprintf(stderr, "Error: %s did not respond within %.1fs\n",
		what, timeout_ms / 1000.0);
	die("boot stage timeout");
}

/* ---- two-stage boot (ROM -> SPL -> stage2 burner) ----------------------- */
static void boot_device(int verbose)
{
	unsigned char info[8];
	if (get_cpu_info(info) != 8) die("boot ROM did not answer GET_CPU_INFO");
	if (verbose) fprintf(stderr, "Boot ROM up; loading ginfo+SPL\n");

	/* Drive any requested GPIOs while still in boot-ROM mode, before SPL. */
	if (n_gpios) apply_gpios(verbose);

	upload(GINFO_ADDR, fw_ginfo, fw_ginfo_len);
	upload(SPL_ADDR, fw_spl, fw_spl_len);

	set_len(D2I_LEN);
	ctrl_out(VR_PROGRAM_START1, SPL_ADDR >> 16, SPL_ADDR & 0xFFFF, NULL, 0);
	wait_cpu_info(BOOT_WAIT_MS, "SPL");
	if (verbose) fprintf(stderr, "SPL up; loading stage2\n");

	upload(STAGE2_ADDR, fw_uboot, fw_uboot_len);
	ctrl_out(VR_FLUSH_CACHES, 0, 0, NULL, 0);
	ctrl_out(VR_PROGRAM_START2, STAGE2_ADDR >> 16, STAGE2_ADDR & 0xFFFF, NULL, 0);
	wait_cpu_info(BOOT_WAIT_MS, "stage2 burner");
	if (verbose) fprintf(stderr, "Stage2 burner up\n");
}

static void update_cfg(const unsigned char *ep0, unsigned ep0_len,
		       const unsigned char *bulk, unsigned bulk_len)
{
	ctrl_out(VR_UPDATE_CFG, 0, 0, ep0, (uint16_t)ep0_len);
	bulk_out(bulk, bulk_len);
	int32_t ack = get_ack(CTRL_TIMEOUT);
	if (ack != 0) {
		fprintf(stderr, "Error: UPDATE_CFG failed: ack=%d\n", ack);
		die("config rejected");
	}
}

/* ---- commands ----------------------------------------------------------- */
static int cmd_detect(void)
{
	if (!open_device()) {
		printf("No Ingenic device found in USB boot mode.\n");
		printf("  Expected VID:PID 0x%04x:0x%04x\n", INGENIC_VID, BOOT_PID);
		return 1;
	}
	printf("Device found! VID:PID 0x%04x:0x%04x\n", INGENIC_VID, BOOT_PID);
	unsigned char info[8];
	if (get_cpu_info(info) == 8) {
		printf("  CPU info: ");
		for (int i = 0; i < 8; i++) printf("%02x", info[i]);
		printf("  \"");
		for (int i = 0; i < 8; i++) putchar((info[i] >= 32 && info[i] < 127) ? info[i] : '.');
		printf("\"\n");
	} else {
		printf("  CPU info: failed (try power-cycling the device)\n");
	}
	return 0;
}

static int cmd_flash(const char *path, uint32_t offset, int erase_all,
		     int reboot, int verbose, double wait)
{
	/* Read image */
	FILE *f = fopen(path, "rb");
	if (!f) die("cannot open firmware image");
	fseek(f, 0, SEEK_END);
	long total = ftell(f);
	fseek(f, 0, SEEK_SET);
	if (total <= 0) die("firmware image is empty");
	unsigned char *img = malloc((size_t)total);
	if (!img) die("out of memory");
	if (fread(img, 1, (size_t)total, f) != (size_t)total) die("short read on image");
	fclose(f);

	if (!open_device_wait(wait)) die("no Ingenic device found in USB boot mode");

	boot_device(verbose);

	/* cfg #1 (policy) */
	update_cfg(fw_cfg1_ep0, fw_cfg1_ep0_len, fw_cfg1_bulk, fw_cfg1_bulk_len);

	/* Read the live JEDEC and pick the matching embedded cfg2. */
	unsigned char jr[3];
	if (ctrl_in(VR_GET_FLASH_INFO, jr, 3) != 3) die("GET_FLASH_INFO failed");
	uint32_t jedec = (uint32_t)jr[2] << 16 | jr[1] << 8 | jr[0];

	const struct nor_chip *sel = NULL;
	for (int i = 0; i < nor_chips_count; i++)
		if (nor_chips[i].jedec == jedec) { sel = &nor_chips[i]; break; }
	if (!sel) {
		fprintf(stderr, "Flash JEDEC ID: 0x%06x\n", jedec);
		fprintf(stderr, "Error: chip 0x%06x not supported by this build.\n"
				"       Supported:", jedec);
		for (int i = 0; i < nor_chips_count; i++)
			fprintf(stderr, " 0x%06x(%s)", nor_chips[i].jedec, nor_chips[i].name);
		fprintf(stderr, "\n       Add it: gen_blob.py 0x%06x ... && make\n", jedec);
		die("unsupported flash chip");
	}
	fprintf(stderr, "Flash JEDEC ID: 0x%06x (%s)\n", jedec, sel->name);

	/* cfg #2 (chip params): sector-erase by default, chip-erase if asked */
	if (erase_all)
		update_cfg(fw_cfg2_ep0, fw_cfg2_ep0_len, sel->cfg2_chip, sel->cfg2_chip_len);
	else
		update_cfg(fw_cfg2_ep0, fw_cfg2_ep0_len, sel->cfg2_sector, sel->cfg2_sector_len);

	/* INIT — triggers chip erase when in chip-erase mode; poll the ACK */
	ctrl_out(VR_INIT, 0, 0, NULL, 0);
	int max_polls = 15 + (int)(total / CHUNK);
	if (max_polls < 30) max_polls = 30;
	struct timespec two_s = { 2, 0 };
	int done = 0;
	for (int i = 0; i < max_polls; i++) {
		nanosleep(&two_s, NULL);
		int32_t ack = get_ack(CTRL_TIMEOUT);
		if (ack == 0) { done = 1; break; }
		if (ack != -16 && ack != INT32_MIN) { /* not EBUSY, not "no ack" */
			fprintf(stderr, "Error: flash init failed: ack=%d\n", ack);
			die("flash init error");
		}
	}
	if (!done) die("flash init timed out");
	if (verbose) fprintf(stderr, "Flash initialized\n");

	/* Stream the image in 64K chunks */
	set_len((uint32_t)total);
	fprintf(stderr, "Writing %ld bytes at offset 0x%x\n", total, offset);

	long sent = 0;
	while (sent < total) {
		uint32_t n = (uint32_t)((total - sent > CHUNK) ? CHUNK : (total - sent));
		uint32_t crc = crc32_inv(img + sent, n);

		/* 40-byte write command (see protocol.py) */
		unsigned char cmd[40];
		memset(cmd, 0, sizeof(cmd));
		uint32_t off = offset + (uint32_t)sent;
		put_le32(cmd + 8,  off);            /* dst offset */
		put_le32(cmd + 16, n);              /* length */
		put_le32(cmd + 24, FLASH_OPS_NOR);  /* flash ops */
		put_le32(cmd + 28, crc);            /* CRC */

		ctrl_out(VR_WRITE, 0, 0, cmd, sizeof(cmd));
		bulk_out(img + sent, n);

		int ack_to = (int)(n / 32) * 1000;     /* ~1s per 32K */
		if (ack_to < 10000) ack_to = 10000;
		int32_t ack = get_ack(ack_to);
		if (ack != 0) {
			fprintf(stderr, "\nError: write failed at 0x%x: ack=%d\n",
				off, ack);
			die("write error");
		}
		sent += n;

		if (verbose) {
			fprintf(stderr, "write 0x%08x  %ld/%ld\n", off, sent, total);
		} else {
			int pct = (int)(sent * 100 / total);
			int filled = (int)(sent * 40 / total);
			fprintf(stderr, "\r  [");
			for (int i = 0; i < 40; i++) fputc(i < filled ? '#' : '-', stderr);
			fprintf(stderr, "] %d%% (%ld/%ld)", pct, sent, total);
		}
	}
	if (!verbose) fprintf(stderr, "\n");
	free(img);

	if (reboot) {
		if (verbose) fprintf(stderr, "Rebooting device\n");
		/* device reboots immediately; ignore transfer errors */
		libusb_control_transfer(dev, REQ_OUT, VR_REBOOT, 0, 0, NULL, 0, CTRL_TIMEOUT);
	}
	printf("Flash complete!\n");
	return 0;
}

/* ---- main --------------------------------------------------------------- */
static void usage(void)
{
	fprintf(stderr,
		"Usage:\n"
		"  ingenic_flash detect\n"
		"  ingenic_flash flash <image> [--offset N] [--erase-all] [--no-reboot]\n"
		"                      [--wait SECONDS] [--gpio PORT STATE]... [-v]\n"
		"\n"
		"  --wait SECONDS     poll up to SECONDS for the device to enter USB\n"
		"                     boot mode before flashing (default 15, 0 = no wait)\n"
		"  --gpio PORT STATE  drive a GPIO via the boot ROM before SPL load\n"
		"                     (repeatable), e.g. --gpio PB15 on  to assert a\n"
		"                     PMIC power-hold line. PORT=P<A-D><0-31>, STATE=on/off.\n");
}

int main(int argc, char **argv)
{
	if (argc < 2) { usage(); return 2; }

	int rc = libusb_init(&ctx);
	if (rc < 0) die_usb("libusb_init", rc);

	int ret;
	if (!strcmp(argv[1], "detect")) {
		ret = cmd_detect();
	} else if (!strcmp(argv[1], "flash")) {
		if (argc < 3) { usage(); ret = 2; goto out; }
		const char *image = argv[2];
		uint32_t offset = 0;
		int erase_all = 0, reboot = 1, verbose = 0;
		double wait = 15.0;
		for (int i = 3; i < argc; i++) {
			if (!strcmp(argv[i], "--offset") && i + 1 < argc)
				offset = (uint32_t)strtoul(argv[++i], NULL, 0);
			else if (!strcmp(argv[i], "--wait") && i + 1 < argc)
				wait = strtod(argv[++i], NULL);
			else if (!strcmp(argv[i], "--erase-all")) erase_all = 1;
			else if (!strcmp(argv[i], "--no-reboot")) reboot = 0;
			else if (!strcmp(argv[i], "-v")) verbose = 1;
			else if (!strcmp(argv[i], "--gpio") && i + 2 < argc) {
				if (parse_gpio(argv[i + 1], argv[i + 2]) < 0) {
					fprintf(stderr, "Invalid --gpio %s %s\n",
						argv[i + 1], argv[i + 2]);
					ret = 2; goto out;
				}
				i += 2;
			}
			else { fprintf(stderr, "Unknown arg: %s\n", argv[i]); usage(); ret = 2; goto out; }
		}
		ret = cmd_flash(image, offset, erase_all, reboot, verbose, wait);
	} else {
		usage();
		ret = 2;
	}

out:
	if (dev) libusb_close(dev);
	libusb_exit(ctx);
	return ret;
}
