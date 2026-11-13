// Luma's camera overlay: a transparent webOS "overlay" window, so the app underneath keeps
// playing. Luma launches (or relaunches) it with the camera details as launch parameters and
// shows one doorbell-style card, or (mode "video") an enlarged camera from Luma's Cameras app as
// Home Assistant's HLS video, full screen, decoded by the TV. OK on a card expands it to full
// screen; Luma then relaunches the page with that camera's HLS video when it has one.
(function () {
  'use strict';
  var logic = window.LumaOverlay;
  var card = document.getElementById('card');
  var feed = document.getElementById('feed');
  var status = document.getElementById('status');
  var bar = document.getElementById('bar');
  var videoView = document.getElementById('video');
  var player = document.getElementById('player');
  var videoStatus = document.getElementById('video-status');
  var videoLive = document.getElementById('video-live');
  // A video that shows nothing by then gives the screen back to Luma's motion-JPEG stream.
  var VIDEO_WAIT_MS = 15000;
  var hls = null;
  var current = null;
  // The doorbell card on screen, kept while its expanded camera's video starts.
  var shown = null;
  var expanded = false;
  var countdown = null;
  var timers = [];
  var closing = false;
  var closeTimer = null;

  function launchParams() {
    var raw;
    try {
      raw =
        (window.PalmSystem && window.PalmSystem.launchParams) ||
        (window.webOSSystem && window.webOSSystem.launchParams);
    } catch (_error) {
      raw = null;
    }
    if (typeof raw === 'string') {
      try {
        return JSON.parse(raw || '{}');
      } catch (_error) {
        return {};
      }
    }
    return raw || {};
  }

  function clearTimers() {
    clearTimeout(countdown);
    countdown = null;
    timers.forEach(function (timer) {
      clearTimeout(timer);
      clearInterval(timer);
    });
    timers = [];
  }

  function showStatus(text) {
    status.textContent = text;
    status.hidden = !text;
  }

  // Tell Luma the overlay closed so it can resume paused playback. Opaque "no-cors" requests
  // need no CORS reply; Luma accepts this endpoint only from the TV itself with this nonce.
  // `failed`: a camera video could not play, so the Cameras app keeps its own stream on screen.
  function report(settings, failed) {
    send(settings, logic.closedUrl, { failed: failed === true });
  }

  function send(settings, address, extra) {
    if (!settings || !settings.session) return;
    var url = address(settings.port);
    var fields = { session: settings.session, nonce: settings.nonce };
    Object.keys(extra).forEach(function (key) {
      fields[key] = extra[key];
    });
    var body = JSON.stringify(fields);
    try {
      if (navigator.sendBeacon && navigator.sendBeacon(url, body)) return;
    } catch (_error) {
      // Fall through to fetch.
    }
    try {
      fetch(url, { method: 'POST', mode: 'no-cors', body: body, keepalive: true });
    } catch (_error) {
      // Luma also ends the session after the overlay's duration.
    }
  }

  function dismiss(failed) {
    if (closing) return;
    closing = true;
    clearTimers();
    feed.removeAttribute('src');
    card.hidden = true;
    shown = null;
    expanded = false;
    stopVideo();
    report(current, failed);
    closeTimer = setTimeout(function () {
      window.close();
    }, 150);
  }

  // A page suspended while hidden may resume through a relaunch before its close fires.
  function cancelClose() {
    clearTimeout(closeTimer);
    closeTimer = null;
  }

  function snapshots(settings) {
    var loading = false;
    function next() {
      if (loading) return;
      loading = true;
      var image = new Image();
      image.onload = function () {
        loading = false;
        feed.src = image.src;
        showStatus('');
      };
      image.onerror = function () {
        loading = false;
        showStatus('Camera unavailable');
      };
      image.src = logic.frameUrl(settings.snapshot, Date.now());
    }
    next();
    timers.push(setInterval(next, settings.refresh * 1000));
  }

  // ---- Camera video (Cameras app) ----------------------------------------------------------

  function stopVideo() {
    if (hls) {
      hls.destroy();
      hls = null;
    }
    player.onplaying = null;
    player.onerror = null;
    player.pause();
    player.removeAttribute('src');
    try {
      player.load();
    } catch (_error) {
      // Nothing was loading.
    }
    videoView.hidden = true;
    videoView.className = 'video';
  }

  function start() {
    var playing = player.play();
    // Autoplay with sound can be refused; play muted rather than not at all.
    if (playing && playing.catch) {
      playing.catch(function () {
        player.muted = true;
        player.play();
      });
    }
  }

  function loadHls(done) {
    if (window.Hls) return done(window.Hls);
    var script = document.createElement('script');
    script.src = 'vendor/hls.light.min.js';
    script.onload = function () {
      done(window.Hls);
    };
    script.onerror = function () {
      done(null);
    };
    document.head.appendChild(script);
  }

  // hls.js plays HLS through Media Source Extensions where the TV's own player cannot.
  function playWithHlsJs(settings) {
    loadHls(function (Hls) {
      if (closing || current !== settings) return;
      if (!Hls || !Hls.isSupported()) {
        videoFailed(settings);
        return;
      }
      player.removeAttribute('src');
      hls = new Hls({ lowLatencyMode: true, backBufferLength: 10 });
      hls.on(Hls.Events.ERROR, function (_event, data) {
        if (data && data.fatal) videoFailed(settings);
      });
      hls.on(Hls.Events.MANIFEST_PARSED, start);
      hls.loadSource(settings.url);
      hls.attachMedia(player);
    });
  }

  // The camera's own feed full screen: OK on a doorbell card. The countdown stops, and Luma
  // looks for the camera's HLS video meanwhile.
  function expand() {
    expanded = true;
    clearTimeout(countdown);
    countdown = null;
    card.className = 'card expanded';
    send(current, logic.expandUrl, {});
  }

  // An expanded card's video that cannot play leaves its full-screen feed on screen.
  function videoFailed(settings) {
    if (!settings.expanded || !shown) {
      dismiss(true);
      return;
    }
    var doorbell = shown;
    clearTimers();
    stopVideo();
    show(doorbell, true);
  }

  function playVideo(settings) {
    cancelClose();
    // An expanded card stays on screen, feed running, until the video plays over it.
    var keepCard = settings.expanded && expanded && shown && !card.hidden;
    if (!keepCard) {
      clearTimers();
      card.hidden = true;
      shown = null;
      expanded = false;
    }
    stopVideo();
    closing = false;
    current = settings;
    document.getElementById('video-title').textContent = settings.title;
    videoStatus.hidden = false;
    videoLive.hidden = true;
    videoView.hidden = false;
    var playing = false;
    // The expanded card's own badge stays until then.
    videoStatus.hidden = keepCard;
    player.onplaying = function () {
      playing = true;
      videoView.className = 'video playing';
      videoStatus.hidden = true;
      videoLive.hidden = false;
      if (!card.hidden) {
        clearTimers();
        feed.removeAttribute('src');
        card.hidden = true;
      }
    };
    timers.push(
      setTimeout(function () {
        if (!playing) videoFailed(settings);
      }, VIDEO_WAIT_MS)
    );
    // The TV's own player first, where it says it plays HLS; hls.js otherwise or if it fails.
    var native = player.canPlayType && player.canPlayType('application/vnd.apple.mpegurl');
    if (native) {
      player.onerror = function () {
        player.onerror = null;
        if (playing) videoFailed(settings);
        else playWithHlsJs(settings);
      };
      player.src = settings.url;
      start();
    } else {
      playWithHlsJs(settings);
    }
  }

  function show(settings, full) {
    cancelClose();
    clearTimers();
    stopVideo();
    closing = false;
    current = settings;
    shown = settings;
    expanded = full === true;
    card.className = expanded ? 'card expanded' : 'card ' + settings.position + ' ' + settings.size;
    document.getElementById('title').textContent = settings.title;
    var message = document.getElementById('message');
    message.textContent = settings.message;
    message.hidden = !settings.message;
    showStatus('Connecting to camera…');
    feed.onload = function () {
      showStatus('');
    };
    feed.onerror = function () {
      // A motion-JPEG stream that fails falls back to still snapshots when one was given.
      // Snapshot frames are decoded before being shown, so they never reach this handler.
      feed.onerror = null;
      if (settings.stream && settings.snapshot) {
        snapshots(settings);
      } else {
        showStatus('Camera unavailable');
      }
    };
    if (settings.stream) {
      feed.src = settings.stream;
    } else {
      snapshots(settings);
    }
    // Restart the countdown animation for a replaced overlay.
    bar.style.transition = 'none';
    bar.style.transform = 'scaleX(1)';
    void bar.offsetWidth;
    bar.style.transition = 'transform ' + settings.duration + 's linear';
    bar.style.transform = 'scaleX(0)';
    card.hidden = false;
    if (!expanded) {
      countdown = setTimeout(function () {
        dismiss();
      }, settings.duration * 1000);
    }
  }

  function apply(params) {
    var video = logic.video(params);
    if (video) {
      playVideo(video);
      return;
    }
    var settings = logic.settings(params);
    if (!settings || settings.dismiss) {
      dismiss();
      return;
    }
    show(settings);
  }

  document.addEventListener('keydown', function (event) {
    // 461 is the webOS remote's Back key; Escape helps when testing in a desktop browser.
    // OK expands a doorbell card to full screen; full screen, it closes, as an enlarged
    // camera does in Luma.
    var ok = event.keyCode === 13;
    var full = !videoView.hidden || expanded;
    if (event.keyCode === 461 || event.keyCode === 27 || (full && ok)) {
      event.preventDefault();
      dismiss();
    } else if (ok && !card.hidden) {
      event.preventDefault();
      expand();
    }
  });
  window.addEventListener('pagehide', function () {
    report(current);
  });

  document.addEventListener('webOSRelaunch', function (event) {
    apply((event && event.detail) || launchParams());
  });
  apply(launchParams());
})();
