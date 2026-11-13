package com.zohar.autopilot;

import java.util.Base64;

/** Desktop-JVM JPEG gates. No Android SDK, Gradle, camera, or photos. */
public final class JpegGateAssertions {

    private JpegGateAssertions() {}

    public static void runAll() {
        assertEquals("max base64 chars", 45_000_000, JpegGate.MAX_BASE64_CHARS);
        assertEquals("max bytes", 32 * 1024 * 1024, JpegGate.MAX_BYTES);
        assertEquals("max pixels", 40_000_000, JpegGate.MAX_PIXELS);

        byte[] ok = sofJpeg(3, 2);
        JpegGate.Result decoded = JpegGate.decode(Base64.getEncoder().encodeToString(ok));
        assertTrue("3x2 SOF accepted", decoded != null && decoded.width == 3 && decoded.height == 2);
        assertTrue("original bytes", java.util.Arrays.equals(ok, decoded.bytes));

        assertTrue("1x1 accepted", JpegGate.validate(sofJpeg(1, 1)) != null);
        assertTrue("8000x5000 is 40 MP inclusive", JpegGate.validate(sofJpeg(8000, 5000)) != null);
        assertTrue("8001x5000 rejected", JpegGate.validate(sofJpeg(8001, 5000)) == null);
        assertTrue("zero width rejected", JpegGate.validate(sofJpeg(0, 10)) == null);
        assertTrue("missing magic rejected", JpegGate.validate(new byte[] {0x00, 0x01, 0x02}) == null);
        assertTrue("SOI only rejected", JpegGate.validate(new byte[] {(byte) 0xff, (byte) 0xd8, (byte) 0xff, (byte) 0xd9}) == null);
        assertTrue("null rejected", JpegGate.decode(null) == null);
        assertTrue("invalid base64 rejected", JpegGate.decode("not-jpeg") == null);
        assertTrue("empty rejected", JpegGate.decode("") == null);

        byte[] huge = new byte[JpegGate.MAX_BYTES + 1];
        huge[0] = (byte) 0xff;
        huge[1] = (byte) 0xd8;
        huge[2] = (byte) 0xff;
        assertTrue("33 MiB rejected before SOF", JpegGate.validate(huge) == null);
    }

    static byte[] sofJpeg(int width, int height) {
        byte[] jpeg = new byte[] {
            (byte) 0xFF, (byte) 0xD8,
            (byte) 0xFF, (byte) 0xE0, 0x00, 0x10,
            'J', 'F', 'I', 'F', 0, 1, 1, 0, 0, 1, 0, 1, 0, 0,
            (byte) 0xFF, (byte) 0xC0, 0x00, 0x0B, 0x08,
            0, 0, 0, 0,
            1, 1, 0x11, 0,
            (byte) 0xFF, (byte) 0xD9
        };
        jpeg[25] = (byte) ((height >> 8) & 0xff);
        jpeg[26] = (byte) (height & 0xff);
        jpeg[27] = (byte) ((width >> 8) & 0xff);
        jpeg[28] = (byte) (width & 0xff);
        return jpeg;
    }

    private static void assertTrue(String label, boolean value) {
        if (!value) {
            throw new AssertionError(label);
        }
    }

    private static void assertEquals(String label, int expected, int actual) {
        if (expected != actual) {
            throw new AssertionError(label + ": expected " + expected + " got " + actual);
        }
    }

    public static void main(String[] args) {
        runAll();
        System.out.println("PASS: JpegGate JVM assertions");
    }
}
