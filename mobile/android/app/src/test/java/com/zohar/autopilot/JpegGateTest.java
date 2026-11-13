package com.zohar.autopilot;

import org.junit.Test;

/** Local Gradle JUnit wrapper. Hosted CI does not run Gradle. */
public class JpegGateTest {

    @Test
    public void jpegGatesMatchIosLimits() {
        JpegGateAssertions.runAll();
    }
}
