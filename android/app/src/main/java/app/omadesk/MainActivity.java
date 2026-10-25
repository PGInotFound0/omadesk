package app.omadesk;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.Uri;
import android.os.Bundle;
import android.view.View;
import android.view.WindowManager;
import android.view.inputmethod.EditorInfo;
import android.webkit.ConsoleMessage;
import android.webkit.CookieManager;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.Toast;

import java.util.concurrent.atomic.AtomicReference;

/**
 * Minimal WebView front-end for omadesk. No data collection; stores one
 * preference: the viewer URL (including its #token fragment) on this device.
 */
public class MainActivity extends Activity {

    private static final String PREFS = "omadesk";
    private static final String KEY_URL = "url";

    private WebView webView;
    private LinearLayout formView;
    private View gearButton;
    private AtomicReference<String> pendingUrl = new AtomicReference<>(null);

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        // keep the screen on while viewing (AnyDesk-like behavior)
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);

        SharedPreferences prefs = getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        String saved = sanitize(prefs.getString(KEY_URL, ""));

        // support omadesk://<the rest of an http url>
        Uri uri = getIntent() != null ? getIntent().getData() : null;
        if (uri != null && "omadesk".equals(uri.getScheme())) {
            String rest = uri.getEncodedSchemeSpecificPart();
            if (rest != null && rest.startsWith("//")) {
                saved = sanitize("http:" + rest);
            }
        }

        buildUi(prefs, saved);
        if (saved != null) {
            loadInWebView(saved);
        }
    }

    private void buildUi(SharedPreferences prefs, String initialUrl) {
        // --- form: one field + save button ---
        formView = new LinearLayout(this);
        formView.setOrientation(LinearLayout.VERTICAL);
        formView.setPadding(48, 96, 48, 48);

        final EditText input = new EditText(this);
        input.setHint("http://<desktop>:9983/#<token>");
        input.setImeOptions(EditorInfo.IME_ACTION_DONE);
        input.setSingleLine(true);
        if (initialUrl != null) {
            input.setText(initialUrl);
        }
        formView.addView(input);

        Button save = new Button(this);
        save.setText(getString(R.string.connect));
        save.setOnClickListener(v -> {
            String url = sanitize(input.getText().toString().trim());
            if (url == null) {
                Toast.makeText(this, R.string.bad_url, Toast.LENGTH_SHORT).show();
                return;
            }
            prefs.edit().putString(KEY_URL, url).apply();
            formView.setVisibility(View.GONE);
            if (gearButton != null) gearButton.setVisibility(View.VISIBLE);
            loadInWebView(url);
        });
        formView.addView(save);

        // --- settings gear to edit URL later ---
        gearButton = getLayoutInflater().inflate(R.layout.gear_button, null);
        gearButton.setOnClickListener(v -> {
            input.setText(prefs.getString(KEY_URL, ""));
            formView.setVisibility(View.VISIBLE);
            gearButton.setVisibility(View.GONE);
        });

        setContentView(formView);
        addContentView(gearButton, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));
        gearButton.setVisibility(initialUrl != null ? View.VISIBLE : View.GONE);

        if (initialUrl != null) {
            formView.setVisibility(View.GONE);
        }
    }

    private void loadInWebView(String url) {
        if (webView == null) {
            webView = new WebView(this);
            WebSettings s = webView.getSettings();
            s.setJavaScriptEnabled(true);
            s.setDomStorageEnabled(true);
            s.setMediaPlaybackRequiresUserGesture(false);
            s.setLoadWithOverviewMode(true);
            s.setUseWideViewPort(true);
            webView.setBackgroundColor(0xFF12090D);
            webView.setWebViewClient(new WebViewClient() {
                @Override
                public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                    Uri u = request.getUrl();
                    // keep omadesk pages in-app; push other schemes out
                    if (!"http".equals(u.getScheme()) && !"https".equals(u.getScheme())) {
                        try {
                            startActivity(new Intent(Intent.ACTION_VIEW, u));
                        } catch (Exception ignored) { }
                        return true;
                    }
                    return false;
                }
            });
            webView.setWebChromeClient(new WebChromeClient() {
                @Override
                public boolean onConsoleMessage(ConsoleMessage m) {
                    return true;
                }
            });
            CookieManager.getInstance().setAcceptCookie(true);
            setContentView(webView);
            addContentView(gearButton, new LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.WRAP_CONTENT,
                    LinearLayout.LayoutParams.WRAP_CONTENT));
        }
        pendingUrl.set(url);
        webView.loadUrl(url);
    }

    private static String sanitize(String raw) {
        if (raw == null) return null;
        raw = raw.trim();
        if (raw.isEmpty()) return null;
        if (!raw.startsWith("http://") && !raw.startsWith("https://")) {
            raw = "http://" + raw;
        }
        return raw;
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }

    @Override
    public void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        Uri uri = intent.getData();
        if (uri != null && "omadesk".equals(uri.getScheme())) {
            String rest = uri.getEncodedSchemeSpecificPart();
            if (rest != null && rest.startsWith("//")) {
                loadInWebView(sanitize("http:" + rest));
            }
        }
    }

    @Override
    protected void onDestroy() {
        if (webView != null) {
            webView.destroy();
            webView = null;
        }
        super.onDestroy();
    }
}
