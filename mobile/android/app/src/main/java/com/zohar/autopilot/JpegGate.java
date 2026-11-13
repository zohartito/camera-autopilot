package com.zohar.autopilot;

import java.util.Base64;

/**
 * JPEG gates identical to {@code AutopilotMediaPlugin.jpegData} on iOS.
 *
 * Parses SOF width/height from the original bytes. Does not decode pixels and
 * never recompresses; callers must write {@link Result#bytes} unchanged.
 */
public final class JpegGate {

    public static final int MAX_BASE64_CHARS = 45_000_000;
    public static final int MAX_BYTES = 32 * 1024 * 1024;
    public static final int MAX_PIXELS = 40_000_000;
    public static final String REJECT = "Expected a JPEG up to 32 MiB and 40 megapixels.";

    public static final class Result {
        public final byte[] bytes;
        public final int width;
        public final int height;

        Result(byte[] bytes, int width, int height) {
            this.bytes = bytes;
            this.width = width;
            this.height = height;
        }
    }

    private JpegGate() {}

    public static Result decode(String encoded) {
        if (encoded == null || encoded.length() > MAX_BASE64_CHARS) {
            return null;
        }
        byte[] data;
        try {
            data = Base64.getDecoder().decode(encoded);
        } catch (IllegalArgumentException ex) {
            return null;
        }
        return validate(data);
    }

    public static Result validate(byte[] data) {
        if (data == null || data.length > MAX_BYTES) {
            return null;
        }
        int[] size = jpegSize(data);
        if (size == null) {
            return null;
        }
        int width = size[0];
        int height = size[1];
        if (width <= 0 || height <= 0 || width > MAX_PIXELS / height) {
            return null;
        }
        return new Result(data, width, height);
    }

    /**
     * @return {@code {width, height}} from the first SOF marker, else null
     */
    static int[] jpegSize(byte[] data) {
        if (data.length < 3) {
            return null;
        }
        if ((data[0] & 0xff) != 0xff || (data[1] & 0xff) != 0xd8 || (data[2] & 0xff) != 0xff) {
            return null;
        }
        int i = 2;
        while (i + 1 < data.length) {
            if ((data[i] & 0xff) != 0xff) {
                return null;
            }
            while (i < data.length && (data[i] & 0xff) == 0xff) {
                i++;
            }
            if (i >= data.length) {
                return null;
            }
            int marker = data[i] & 0xff;
            i++;
            if (marker == 0xd8) {
                continue;
            }
            if (marker == 0xd9 || marker == 0xda) {
                return null;
            }
            if (marker >= 0xd0 && marker <= 0xd7) {
                continue;
            }
            if (marker == 0x01) {
                continue;
            }
            if (i + 1 >= data.length) {
                return null;
            }
            int length = ((data[i] & 0xff) << 8) | (data[i + 1] & 0xff);
            if (length < 2 || i + length > data.length) {
                return null;
            }
            boolean sof = marker >= 0xc0 && marker <= 0xcf && marker != 0xc4 && marker != 0xc8 && marker != 0xcc;
            if (sof) {
                if (length < 8 || i + 6 >= data.length) {
                    return null;
                }
                int height = ((data[i + 3] & 0xff) << 8) | (data[i + 4] & 0xff);
                int width = ((data[i + 5] & 0xff) << 8) | (data[i + 6] & 0xff);
                if (width <= 0 || height <= 0) {
                    return null;
                }
                return new int[] {width, height};
            }
            i += length;
        }
        return null;
    }
}
