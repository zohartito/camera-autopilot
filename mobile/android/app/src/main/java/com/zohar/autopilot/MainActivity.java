package com.zohar.autopilot;

import android.os.Bundle;
import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {

    @Override
    public void onCreate(Bundle savedInstanceState) {
        registerPlugin(AutopilotMediaPlugin.class);
        super.onCreate(savedInstanceState);
    }
}
