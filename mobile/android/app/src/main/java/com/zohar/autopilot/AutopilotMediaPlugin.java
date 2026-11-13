package com.zohar.autopilot;

import android.content.ClipData;
import android.content.ContentValues;
import android.content.Intent;
import android.net.Uri;
import android.os.Environment;
import android.provider.MediaStore;
import android.view.View;
import androidx.activity.result.ActivityResult;
import androidx.core.content.FileProvider;
import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsCompat;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.ActivityCallback;
import com.getcapacitor.annotation.CapacitorPlugin;
import java.io.File;
import java.io.FileOutputStream;
import java.io.OutputStream;
import java.util.Locale;
import java.util.UUID;

@CapacitorPlugin(name = "AutopilotMedia")
public class AutopilotMediaPlugin extends Plugin {

    private static final String SHARE_DIR = "AutopilotShare";

    @PluginMethod
    public void safeArea(PluginCall call) {
        getActivity().runOnUiThread(() -> {
            JSObject result = new JSObject();
            View view = getBridge() != null ? getBridge().getWebView() : null;
            WindowInsetsCompat windowInsets = view != null ? ViewCompat.getRootWindowInsets(view) : null;
            float density = getContext().getResources().getDisplayMetrics().density;
            double top = 0;
            double bottom = 0;
            if (windowInsets != null && density > 0f) {
                Insets bars = windowInsets.getInsets(
                    WindowInsetsCompat.Type.systemBars() | WindowInsetsCompat.Type.displayCutout()
                );
                top = bars.top / (double) density;
                bottom = bars.bottom / (double) density;
            }
            result.put("top", top);
            result.put("bottom", bottom);
            call.resolve(result);
        });
    }

    @PluginMethod
    public void savePhoto(PluginCall call) {
        JpegGate.Result jpeg = JpegGate.decode(call.getString("data"));
        if (jpeg == null) {
            call.reject(JpegGate.REJECT);
            return;
        }
        ContentValues values = new ContentValues();
        values.put(MediaStore.Images.Media.DISPLAY_NAME, displayName(call.getString("filename")));
        values.put(MediaStore.Images.Media.MIME_TYPE, "image/jpeg");
        values.put(MediaStore.Images.Media.RELATIVE_PATH, Environment.DIRECTORY_PICTURES + "/Autopilot");
        values.put(MediaStore.Images.Media.IS_PENDING, 1);
        Uri uri = getContext().getContentResolver().insert(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, values);
        if (uri == null) {
            call.reject("Could not save photo.");
            return;
        }
        boolean published = false;
        try (OutputStream stream = getContext().getContentResolver().openOutputStream(uri)) {
            if (stream == null) {
                call.reject("Could not save photo.");
                return;
            }
            stream.write(jpeg.bytes);
            stream.flush();
            ContentValues pending = new ContentValues();
            pending.put(MediaStore.Images.Media.IS_PENDING, 0);
            getContext().getContentResolver().update(uri, pending, null, null);
            published = true;
            JSObject result = new JSObject();
            result.put("saved", true);
            call.resolve(result);
        } catch (Exception ex) {
            call.reject(ex.getMessage() != null ? ex.getMessage() : "Could not save photo.");
        } finally {
            if (!published) {
                getContext().getContentResolver().delete(uri, null, null);
            }
        }
    }

    @PluginMethod
    public void sharePhoto(PluginCall call) {
        JpegGate.Result jpeg = JpegGate.decode(call.getString("data"));
        if (jpeg == null) {
            call.reject(JpegGate.REJECT);
            return;
        }
        File directory = new File(getContext().getCacheDir(), SHARE_DIR);
        if (!directory.exists() && !directory.mkdirs()) {
            call.reject("Could not prepare photo for sharing.");
            return;
        }
        retireOldShares(directory);
        File file = new File(directory, UUID.randomUUID() + ".jpg");
        try (FileOutputStream stream = new FileOutputStream(file)) {
            stream.write(jpeg.bytes);
            stream.flush();
        } catch (Exception ex) {
            //noinspection ResultOfMethodCallIgnored
            file.delete();
            call.reject("Could not prepare photo for sharing.");
            return;
        }
        Uri uri;
        try {
            uri = FileProvider.getUriForFile(getContext(), getContext().getPackageName() + ".fileprovider", file);
        } catch (IllegalArgumentException ex) {
            //noinspection ResultOfMethodCallIgnored
            file.delete();
            call.reject("Could not prepare photo for sharing.");
            return;
        }
        Intent send = new Intent(Intent.ACTION_SEND);
        send.setType("image/jpeg");
        send.putExtra(Intent.EXTRA_STREAM, uri);
        send.setClipData(ClipData.newRawUri("", uri));
        send.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
        Intent chooser = Intent.createChooser(send, "Share");
        chooser.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
        startActivityForResult(call, chooser, "shareFinished");
    }

    @ActivityCallback
    private void shareFinished(PluginCall call, ActivityResult result) {
        if (call == null) {
            return;
        }
        // ACTION_SEND choosers typically return RESULT_CANCELED even after a target
        // is chosen. That is not iOS cancel, and deleting the cache URI here would
        // race the receiving app. retireOldShares() drops files older than an hour.
        JSObject out = new JSObject();
        out.put("shared", false);
        out.put("cancelled", false);
        out.put("completedUnknown", true);
        call.resolve(out);
    }

    static String displayName(String filename) {
        if (filename == null) {
            return "autopilot.jpg";
        }
        String name = filename.replace('\\', '/');
        int slash = name.lastIndexOf('/');
        if (slash >= 0) {
            name = name.substring(slash + 1);
        }
        if (name.isEmpty() || ".".equals(name) || "..".equals(name) || !name.matches("[A-Za-z0-9._-]+")) {
            return "autopilot.jpg";
        }
        String lower = name.toLowerCase(Locale.US);
        if (!lower.endsWith(".jpg") && !lower.endsWith(".jpeg")) {
            name = name + ".jpg";
        }
        return name;
    }

    private static void retireOldShares(File directory) {
        File[] files = directory.listFiles();
        if (files == null) {
            return;
        }
        long cutoff = System.currentTimeMillis() - 3600_000L;
        for (File file : files) {
            if (file.lastModified() < cutoff) {
                //noinspection ResultOfMethodCallIgnored
                file.delete();
            }
        }
    }
}
