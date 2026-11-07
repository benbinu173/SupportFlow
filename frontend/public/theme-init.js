// Resolve the theme before the first paint.
//
// This classic script blocks parsing before the body. React effects run
// after commit, so doing this work in ThemeProvider would paint one light
// frame for every dark-mode user on every load — the exact flash the
// attribute exists to prevent.
//
// The storage key and the resolution rule are duplicated from
// src/store/theme.ts, which this cannot import. Keep the two in step.
;(function () {
  var theme = 'light'

  try {
    var stored = localStorage.getItem('supportflow.theme')

    theme =
      stored === 'light' || stored === 'dark'
        ? stored
        : window.matchMedia('(prefers-color-scheme: dark)').matches
          ? 'dark'
          : 'light'
  } catch {
    // Storage can throw rather than return null — Safari in private mode
    // denies it, as can an embedded webview. Light is the default either
    // way; failing here would leave the page unstyled instead.
  }

  document.documentElement.dataset.theme = theme
})()
