//! Root-only TV calls for the Home Assistant camera overlay: install the overlay app bundled in
//! Luma's package, close it, and pause or resume the foreground media pipeline around it.
use crate::{
    Result, accepted,
    system::{self, System},
};
use serde_json::{Value, json};
use std::path::Path;

pub const OVERLAY: &str = "com.ecklf.luma.overlay";
/// Inside Luma's installation; `tools/package.mjs` builds and stages both files.
pub const BUNDLE: &str = "overlay";
/// The overlay IPK inside Luma's package. ares-package drops any path containing ".ipk" from an
/// app, so it is stored under another name and copied to an `.ipk` path to install.
pub const PACKAGE: &str = "overlay.package";
const STAGED: &str = "/tmp/luma-overlay.ipk";

/// webOS media IDs are short opaque tokens such as `_3FzD9vJx1k2abc`.
pub fn valid_media_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 128
        && id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-' | b'.' | b':'))
}

fn installed_version(system: &impl System) -> Result<Option<String>> {
    let reply = system::luna(
        system,
        "com.webos.applicationManager",
        "getAppInfo",
        &json!({"id": OVERLAY}),
    )?;
    if reply["returnValue"] != true {
        return Ok(None);
    }
    Ok(reply["appInfo"]["version"].as_str().map(str::to_owned))
}

/// Installs or updates the bundled overlay app when its version differs from the bundle.
fn install(system: &impl System, directory: &Path) -> Result<Value> {
    let bundle = directory.join(BUNDLE);
    let manifest = system::json(system, bundle.join("appinfo.json"))?;
    if manifest["id"] != OVERLAY {
        return Err("The bundled overlay has an unexpected app ID".into());
    }
    let expected = manifest["version"]
        .as_str()
        .ok_or("The bundled overlay has no version")?
        .to_owned();
    if installed_version(system)?.as_deref() == Some(expected.as_str()) {
        return Ok(json!({"returnValue":true,"version":expected,"changed":false}));
    }
    let package = bundle.join(PACKAGE);
    if !system::exists(system, &package) {
        return Err("The Luma package does not include the overlay app".into());
    }
    let bytes = system::read(system, &package, 16 * 1024 * 1024)?;
    system::mkdir(system, "/tmp")?;
    system::write(system, STAGED, &bytes, 0o644)?;
    let result = (|| {
        accepted(system::luna(
            system,
            "com.webos.appInstallService",
            "dev/install",
            &json!({"id": OVERLAY, "ipkUrl": STAGED, "subscribe": false}),
        )?)?;
        // dev/install answers before installing; the app manager lists the result once done.
        for _ in 0..60 {
            system.pause();
            if installed_version(system)?.as_deref() == Some(expected.as_str()) {
                return Ok(json!({"returnValue":true,"version":expected,"changed":true}));
            }
        }
        Err("The overlay app was not installed within a minute".into())
    })();
    let _ = system::remove(system, STAGED);
    result
}

fn close(system: &impl System) -> Result<Value> {
    let reply = system::luna(
        system,
        "com.webos.applicationManager",
        "closeByAppId",
        &json!({"id": OVERLAY}),
    )?;
    // Closing an overlay that already dismissed itself is not an error.
    Ok(json!({"returnValue":true,"closed":reply["returnValue"] == true}))
}

/// Pauses the first playing foreground pipeline and reports its media ID for `resume`.
/// Live TV and players outside the media service have no pipeline here; nothing is paused.
fn pause(system: &impl System) -> Result<Value> {
    let reply = accepted(system::luna(
        system,
        "com.webos.media",
        "getForegroundAppInfo",
        &json!({}),
    )?)?;
    let playing = reply["foregroundAppInfo"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|entry| entry["playState"] == "playing")
        .filter_map(|entry| entry["mediaId"].as_str())
        .find(|id| valid_media_id(id));
    let Some(id) = playing else {
        return Ok(json!({"returnValue":true,"mediaId":null}));
    };
    accepted(system::luna(
        system,
        "com.webos.media",
        "pause",
        &json!({"mediaId": id}),
    )?)?;
    Ok(json!({"returnValue":true,"mediaId":id}))
}

fn resume(system: &impl System, id: &str) -> Result<Value> {
    if !valid_media_id(id) {
        return Err("Invalid media ID".into());
    }
    accepted(system::luna(
        system,
        "com.webos.media",
        "play",
        &json!({"mediaId": id}),
    )?)?;
    Ok(json!({"returnValue":true,"mediaId":id}))
}

/// A system toast over any app, sent as Luma: the notification service requires that
/// `sourceId` is the calling app, which root `luna-send -a` may claim.
fn toast(system: &impl System, message: &str) -> Result<Value> {
    if message.trim().is_empty() || message.chars().count() > 300 {
        return Err("Toast messages need 1-300 characters".into());
    }
    let uri = "luna://com.webos.notification/createToast";
    let payload = json!({"sourceId": crate::APP, "message": message, "noaction": true});
    let result = system.run(
        "/usr/bin/luna-send",
        &[
            "-n".into(),
            "1".into(),
            "-w".into(),
            "4000".into(),
            "-a".into(),
            crate::APP.into(),
            uri.into(),
            payload.to_string(),
        ],
        std::time::Duration::from_secs(8),
        64 * 1024,
    )?;
    let reply: Value =
        serde_json::from_slice(&result.stdout).map_err(|e| format!("{uri}: invalid JSON: {e}"))?;
    accepted(reply)
}

pub fn dispatch(
    system: &impl System,
    command: &str,
    argument: Option<&str>,
    directory: &Path,
) -> Result<Value> {
    system::root(system)?;
    match command {
        "install" => install(system, directory),
        "close" => close(system),
        "pause" => pause(system),
        "resume" => resume(system, argument.ok_or("Missing media ID")?),
        "toast" => toast(system, argument.ok_or("Missing message")?),
        _ => Err("Unknown overlay command".into()),
    }
}
