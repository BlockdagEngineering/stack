// Command dashboard is a minimal HTMX + Go HTTP UI (DAG block height + mining BDAG/min bar chart).
package main

import (
	"bytes"
	"context"
	"embed"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"html/template"
	"io"
	"log"
	"math"
	"math/big"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
)

//go:embed templates/*.gohtml
var tmplFS embed.FS

//go:embed static/tailwind.css
var embeddedTailwindCSS []byte

//go:embed static/htmx.min.js
var embeddedHTMX []byte

const defaultMiningPoll = 10 * time.Minute

func main() {
	rpcURL := getenv("BDAG_RPC_URL", "http://127.0.0.1:38131")
	rpcUser := getenv("NODE_RPC_USER", getenv("BDAG_RPC_USER", "test"))
	rpcPass := getenv("NODE_RPC_PASS", getenv("BDAG_RPC_PASSWORD", "test"))
	evmRPCURL := strings.TrimRight(getenv("DASHBOARD_EVM_RPC_URL", "http://127.0.0.1:18545"), "/")
	listen := getenv("DASHBOARD_LISTEN", "127.0.0.1:9280")
	pollSecs, _ := strconv.Atoi(getenv("DASHBOARD_RPC_POLL_SECONDS", "5"))
	if pollSecs < 2 {
		pollSecs = 2
	}
	maxPts, _ := strconv.Atoi(getenv("DASHBOARD_MAX_POINTS", "120"))
	if maxPts < 8 {
		maxPts = 8
	}
	maxMiningBars, _ := strconv.Atoi(getenv("DASHBOARD_MINING_MAX_BARS", "48"))
	if maxMiningBars < 4 {
		maxMiningBars = 4
	}

	miningDur := getenvDuration("DASHBOARD_MINING_POLL_INTERVAL", defaultMiningPoll)
	rawWallet, walletSource := resolveMiningWalletRaw()
	walletNorm, walletErr := normalizeWalletAddress(rawWallet)

	mCfg := miningConfig{
		Enabled:     walletNorm != "",
		Address:     walletNorm,
		Source:      walletSource,
		EVMRPC:      evmRPCURL,
		SetupErr:    "",
		MaxRateBars: maxMiningBars,
	}
	if strings.TrimSpace(rawWallet) != "" && walletNorm == "" {
		mCfg.SetupErr = walletErr
		mCfg.Enabled = false
	}

	miningPollSecs := int(miningDur / time.Second)
	if miningPollSecs < 120 {
		miningPollSecs = 120
	}
	miningHx := fmt.Sprintf("%ds", miningPollSecs)

	tmpl := template.Must(template.New("root").Funcs(template.FuncMap{
		"printf": fmt.Sprintf,
	}).ParseFS(tmplFS, "templates/*.gohtml"))

	hStore := newHeightStore(maxPts)
	mStore := newMiningRateStore(maxMiningBars)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go pollHeights(ctx, hStore, rpcURL, rpcUser, rpcPass, time.Duration(pollSecs)*time.Second)
	if mCfg.Enabled {
		go pollMiningBalance(ctx, mStore, mCfg.EVMRPC, mCfg.Address, miningDur)
	}

	view := func() pageView {
		pts, heightErr := hStore.snapshot()
		return buildPageView(pts, heightErr, mStore.snapshot(), mCfg,
			pollSecs, miningHx, miningDur)
	}

	mux := http.NewServeMux()
	mux.HandleFunc("GET /assets/tailwind.css", serveTailwindCSS)
	mux.HandleFunc("GET /assets/htmx.min.js", serveHTMXJS)
	mux.HandleFunc("GET /", func(w http.ResponseWriter, r *http.Request) {
		p := view()
		writeTemplate(w, tmpl, "layout", p)
	})
	mux.HandleFunc("GET /partials/block-height-chart", func(w http.ResponseWriter, r *http.Request) {
		p := view()
		writeTemplate(w, tmpl, "blockHeightChart", p)
	})
	mux.HandleFunc("GET /partials/mining-dashboard", func(w http.ResponseWriter, r *http.Request) {
		p := view()
		writeTemplate(w, tmpl, "miningDashboard", p)
	})

	log.Printf("bdag dashboard listening http://%s (BDAG RPC %s, EVM %s, mining=%v)",
		listen, rpcURL, evmRPCURL, mCfg.Enabled)
	if err := http.ListenAndServe(listen, mux); err != nil {
		log.Fatal(err)
	}
}

func writeTemplate(w http.ResponseWriter, tmpl *template.Template, name string, data any) {
	var buf bytes.Buffer
	if err := tmpl.ExecuteTemplate(&buf, name, data); err != nil {
		log.Printf("template %s: %v", name, err)
		http.Error(w, http.StatusText(http.StatusInternalServerError), http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	_, _ = io.Copy(w, &buf)
}

func serveHTMXJS(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/javascript; charset=utf-8")
	w.Header().Set("Cache-Control", "public, max-age=86400")
	_, _ = w.Write(embeddedHTMX)
}

func serveTailwindCSS(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "text/css; charset=utf-8")
	w.Header().Set("Cache-Control", "public, max-age=3600")
	_, _ = w.Write(embeddedTailwindCSS)
}

func getenv(k, def string) string {
	if v := strings.TrimSpace(os.Getenv(k)); v != "" {
		return v
	}
	return def
}

// getenvDuration reads a duration env; unsupported or empty parses return def.
func getenvDuration(k string, def time.Duration) time.Duration {
	raw := strings.TrimSpace(os.Getenv(k))
	if raw == "" {
		return def
	}
	d, err := time.ParseDuration(raw)
	if err != nil {
		return def
	}
	if d < 2*time.Minute {
		return 2 * time.Minute
	}
	return d
}

func resolveMiningWalletRaw() (value, source string) {

	if x := getenv("MINING_POOL_ADDRESS", ""); x != "" {
		return x, "MINING_POOL_ADDRESS"
	}
	return "", ""
}

// BarChartSampleData is one categorical sample (typically one vertical bar).
type BarChartSampleData struct {
	Time  time.Time
	Value float64
}

// BarChartData is hydrated into layout metrics by BarChartData.Hydrate.
type BarChartData struct {
	Title               string
	HorizontalAxisLabel string
	VerticalAxisLabel   string
	Caption             string
	Samples             []BarChartSampleData
	EmptyMessage        string
	CurrentBalance      string // formatted BDAG; shown under chart when non-empty
}

// Hydrate turns semantic bar-chart input into drawable SVG/CSS fields.
func (d BarChartData) Hydrate() barChartView {
	em := strings.TrimSpace(d.EmptyMessage)
	if em == "" {
		em = "No samples yet."
	}
	base := barChartView{
		Title:               d.Title,
		HorizontalAxisLabel: d.HorizontalAxisLabel,
		VerticalAxisLabel:   d.VerticalAxisLabel,
		Caption:             d.Caption,
		CurrentBalance:      strings.TrimSpace(d.CurrentBalance),
		HasData:             len(d.Samples) > 0,
		EmptyMessage:        em,
	}

	if len(d.Samples) == 0 {
		return base
	}

	const (
		W      = 640
		H      = 300
		padL   = 70
		padR   = 20
		padT   = 36
		padB   = 62
		barGap = 5.0
	)

	base.Width = W
	base.Height = H
	base.InnerX = padL + 14
	base.InnerY = padT
	base.InnerW = float64(W) - base.InnerX - padR
	base.InnerH = float64(H) - padT - padB

	base.YLabelPivotX = 16
	base.YLabelPivotY = padT + base.InnerH/2

	yMin := d.Samples[0].Value
	yMax := d.Samples[0].Value
	for _, s := range d.Samples[1:] {
		if s.Value < yMin {
			yMin = s.Value
		}
		if s.Value > yMax {
			yMax = s.Value
		}
	}
	if yMin > 0 {
		yMin = 0
	}
	if yMin == yMax {
		yMax = yMin + 1
	}
	padSpan := math.Max(math.Abs(yMax-yMin)*0.06, 1e-6)
	yMin -= padSpan * 0.2
	yMax += padSpan
	if yMin == yMax {
		yMax = yMin + 1
	}

	base.YTickNX = base.InnerX - 8
	base.YTickNYMax = base.InnerY + 12
	base.YTickNYMin = base.InnerY + base.InnerH - 1
	base.YTickLabelMax = formatAxisFloat(yMax) + " BDAG/min"
	base.YTickLabelMin = formatAxisFloat(yMin) + " BDAG/min"

	n := len(d.Samples)
	wAvail := base.InnerW - barGap*(float64(n)+1)
	barW := wAvail / float64(n)
	if barW < 4 {
		barW = 4
	}

	spanInv := base.InnerH / (yMax - yMin)

	for i, s := range d.Samples {
		xLeft := base.InnerX + barGap + float64(i)*(barW+barGap)
		hPix := math.Max((s.Value-yMin)*spanInv, 0)
		topY := base.InnerY + base.InnerH - hPix

		base.Bars = append(base.Bars, barElem{X: xLeft, Y: topY, W: barW, H: math.Max(hPix, 1)})

		txt := sampleTimeLabel(s.Time, barW)
		base.XLabels = append(base.XLabels, xLabelGeom{
			X:    xLeft + barW/2,
			Y:    base.InnerY + base.InnerH + 13,
			Text: txt,
		})
	}

	base.HAxisCaptionX = base.InnerX + base.InnerW/2
	base.HAxisCaptionY = float64(H) - 22

	return base
}

func sampleTimeLabel(t time.Time, barWpix float64) string {
	if barWpix < 72 {
		return t.Local().Format("15:04")
	}
	return t.Local().Format("02 15:04")
}

func formatAxisFloat(v float64) string {
	av := math.Abs(v)
	switch {
	case av >= 1000 || (av >= 100 && math.Abs(math.Round(v)-v) < 1e-3):
		return fmt.Sprintf("%.0f", v)
	case av >= 10:
		return fmt.Sprintf("%.1f", v)
	default:
		return fmt.Sprintf("%.2f", v)
	}
}

func formatBalanceBDAG(wei *big.Int) string {
	if wei == nil {
		return ""
	}
	weiPerBdag := new(big.Int).Exp(big.NewInt(10), big.NewInt(18), nil)
	whole := new(big.Int).Quo(wei, weiPerBdag)
	return formatIntWithThousands(whole.String())
}

func formatIntWithThousands(digits string) string {
	s := strings.TrimSpace(digits)
	if s == "" || s == "0" {
		return "0"
	}
	neg := strings.HasPrefix(s, "-")
	if neg {
		s = s[1:]
	}
	n := len(s)
	if n <= 3 {
		if neg {
			return "-" + s
		}
		return s
	}
	first := n % 3
	if first == 0 {
		first = 3
	}
	b := new(strings.Builder)
	b.WriteString(s[:first])
	for i := first; i < n; i += 3 {
		b.WriteByte(',')
		b.WriteString(s[i : i+3])
	}
	out := b.String()
	if neg {
		return "-" + out
	}
	return out
}

type barElem struct {
	X, Y, W, H float64
}

type xLabelGeom struct {
	Text string
	X, Y float64
}

type barChartView struct {
	Title               string
	HorizontalAxisLabel string
	VerticalAxisLabel   string
	Caption             string
	CurrentBalance      string
	HasData             bool
	EmptyMessage        string

	Width, Height                   int
	InnerX, InnerY                  float64
	InnerW, InnerH                  float64
	YLabelPivotX, YLabelPivotY      float64
	YTickNX, YTickNYMax, YTickNYMin float64
	YTickLabelMax, YTickLabelMin    string
	HAxisCaptionX, HAxisCaptionY    float64
	Bars                            []barElem
	XLabels                         []xLabelGeom
}

type miningConfig struct {
	Enabled     bool
	Address     string
	Source      string
	EVMRPC      string
	SetupErr    string
	MaxRateBars int
}

type miningSnap struct {
	RPCErr      string
	RateSamples []BarChartSampleData
	BalanceWei  *big.Int // last successful balance; nil if never polled OK
}

type miningSection struct {
	SetupErr         string
	RPCErr           string
	ConfiguredAddr   bool
	AddressEnvSource string
	AddressShort     string
	AddressFull      string
	PollInterval     string
	Chart            barChartView
}

// pageView drives templates (promoted chart fields + Mining + polling hints).
type pageView struct {
	chartView
	Mining miningSection

	PollInterval       string // block height hx
	MiningPollInterval string // mining hx; e.g. "600s"
}

func miningCaption(pollDur time.Duration) string {
	pt := pollDur.Round(time.Minute)
	if pt <= 0 {
		pt = defaultMiningPoll
	}
	return fmt.Sprintf(
		"Bdag mining rate (%s window): estimated BDAG mined per minute from the change in "+
			"this wallet's native EVM balance between consecutive RPC checks, divided by the "+
			"elapsed minutes (%s spacing). "+
			"Horizontal ticks mark the clock time each balance snapshot was recorded. "+
			"The vertical label \"10 Min Avg\" denotes the average pace over roughly one "+
			"poll window (defaults to 10 minutes), not instantaneous hashrate. "+
			"wallet comes from MINING_POOL_ADDRESS from the stack .env.", pt, pt)
}

func buildPageView(samples []sample, heightRPC string, ms miningSnap, mc miningConfig, heightPollSecs int, miningHx string, miningPollDur time.Duration) pageView {
	interval := fmt.Sprintf("%ds", heightPollSecs)
	mv := miningSection{}
	mv.AddressEnvSource = mc.Source
	mv.PollInterval = miningPollDur.Round(time.Second).String()
	mv.SetupErr = mc.SetupErr
	mv.ConfiguredAddr = mc.Enabled && mc.SetupErr == ""

	if mv.ConfiguredAddr {
		mv.AddressFull = mc.Address
		mv.AddressShort = shortAddress(mc.Address)
	}
	mv.RPCErr = ms.RPCErr

	bc := BarChartData{
		Title:               "Bdags Mined Per Minute",
		HorizontalAxisLabel: "time of balance check",
		VerticalAxisLabel:   "10 min avg (BDAG/min)",
		Caption:             miningCaption(miningPollDur),
		Samples:             ms.RateSamples,
		EmptyMessage:        "Collecting samples… bars appear once two balance polls complete at the configured interval.",
		CurrentBalance:      formatBalanceBDAG(ms.BalanceWei),
	}
	mv.Chart = bc.Hydrate()

	return pageView{
		chartView:          buildChartView(samples, heightPollSecs, heightRPC),
		Mining:             mv,
		PollInterval:       interval,
		MiningPollInterval: miningHx,
	}
}

func shortAddress(hexAddr string) string {
	if len(hexAddr) <= 14 {
		return hexAddr
	}
	return hexAddr[:6] + "…" + hexAddr[len(hexAddr)-4:]
}

func normalizeWalletAddress(s string) (string, string) {
	s = strings.TrimSpace(s)
	if s == "" {
		return "", ""
	}
	var hexPart string
	if strings.HasPrefix(s, "0x") || strings.HasPrefix(s, "0X") {
		hexPart = s[2:]
	} else {
		hexPart = s
	}
	if len(hexPart) != 40 {
		return "", "address must be 20 bytes (40 hex chars), with optional 0x prefix"
	}
	hexPart = strings.ToLower(hexPart)
	for i := 0; i < len(hexPart); i++ {
		c := hexPart[i]
		if c >= '0' && c <= '9' || c >= 'a' && c <= 'f' {
			continue
		}
		return "", "address contains non-hex character"
	}
	return "0x" + hexPart, ""
}

type sample struct {
	T      time.Time
	Height float64
}

type heightStore struct {
	mu      sync.Mutex
	max     int
	points  []sample
	latest  float64
	okCount int
	lastErr string
}

func newHeightStore(max int) *heightStore {
	return &heightStore{max: max, points: make([]sample, 0, max)}
}

func (s *heightStore) add(h float64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.latest = h
	s.okCount++
	s.points = append(s.points, sample{T: time.Now(), Height: h})
	if len(s.points) > s.max {
		s.points = copySlice(s.points[len(s.points)-s.max:])
	}
}

func (s *heightStore) setErr(err string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.lastErr = err
}

func copySlice(ss []sample) []sample {
	out := make([]sample, len(ss))
	copy(out, ss)
	return out
}

// snapshot returns current height samples and the last BDAG RPC error text (if any).
func (s *heightStore) snapshot() ([]sample, string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]sample, len(s.points))
	copy(out, s.points)
	return out, s.lastErr
}

func pollHeights(ctx context.Context, store *heightStore, rpcURL, user, pass string, every time.Duration) {
	cl := &http.Client{Timeout: 15 * time.Second}
	ticker := time.NewTicker(every)
	defer ticker.Stop()
	for {
		h, err := rpcGetBlockCount(cl, rpcURL, user, pass)
		if err != nil {
			store.setErr(err.Error())
		} else {
			store.add(h)
			store.setErr("")
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

func rpcGetBlockCount(cl *http.Client, rpcURL, user, pass string) (float64, error) {
	body := bytes.NewReader([]byte(`{"jsonrpc":"2.0","id":1,"method":"getBlockCount","params":[]}`))
	req, err := http.NewRequestWithContext(context.Background(), http.MethodPost, rpcURL, body)
	if err != nil {
		return 0, err
	}
	req.Header.Set("Content-Type", "application/json")
	if user != "" {
		auth := base64.StdEncoding.EncodeToString([]byte(user + ":" + pass))
		req.Header.Set("Authorization", "Basic "+auth)
	}
	resp, err := cl.Do(req)
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		return 0, err
	}
	var wrap struct {
		Result json.RawMessage `json:"result"`
		Error  any             `json:"error"`
	}
	if err := json.Unmarshal(raw, &wrap); err != nil {
		return 0, err
	}
	if wrap.Error != nil {
		return 0, fmt.Errorf("%v", wrap.Error)
	}
	var n float64
	if err := json.Unmarshal(wrap.Result, &n); err == nil {
		return n, nil
	}
	var ss string
	if err := json.Unmarshal(wrap.Result, &ss); err == nil {
		ss = strings.TrimSpace(strings.TrimPrefix(ss, `0x`))
		ui, parseErr := strconv.ParseUint(ss, 0, 64)
		if parseErr != nil {
			return 0, parseErr
		}
		return float64(ui), nil
	}
	return 0, fmt.Errorf("unexpected getBlockCount result")
}

type miningRateStore struct {
	mu       sync.Mutex
	maxRates int
	lastWei  *big.Int
	lastAt   time.Time
	rates    []BarChartSampleData
	lastRPC  string // error text
}

func newMiningRateStore(max int) *miningRateStore {
	return &miningRateStore{maxRates: max}
}

func (s *miningRateStore) observeBalance(ok bool, wei *big.Int, errMsg string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if ok && wei != nil {
		t := time.Now()
		dtBetween := t.Sub(s.lastAt).Minutes()
		if s.lastWei != nil && dtBetween > 0.01 {
			delta := new(big.Int).Sub(wei, s.lastWei)
			bpm := weiDeltaToBDAGPerMin(delta, dtBetween)
			s.rates = append(s.rates, BarChartSampleData{Time: t, Value: bpm})
			if len(s.rates) > s.maxRates {
				s.rates = append([]BarChartSampleData(nil), s.rates[len(s.rates)-s.maxRates:]...)
			}
		}
		s.lastWei = new(big.Int).Set(wei)
		s.lastAt = t
		s.lastRPC = ""
		return
	}
	if errMsg != "" {
		s.lastRPC = errMsg
		return
	}
	s.lastRPC = ""
}

func (s *miningRateStore) snapshot() miningSnap {
	s.mu.Lock()
	defer s.mu.Unlock()
	cp := append([]BarChartSampleData(nil), s.rates...)
	var bal *big.Int
	if s.lastWei != nil {
		bal = new(big.Int).Set(s.lastWei)
	}
	return miningSnap{RPCErr: s.lastRPC, RateSamples: cp, BalanceWei: bal}
}

func weiDeltaToBDAGPerMin(delta *big.Int, elapsedMinutes float64) float64 {
	if elapsedMinutes < 1e-6 || delta == nil {
		return 0
	}
	rat := new(big.Rat).SetInt(delta)
	weiPerBdag := new(big.Int).Exp(big.NewInt(10), big.NewInt(18), nil)
	rat.Quo(rat, new(big.Rat).SetInt(weiPerBdag))
	rat.Quo(rat, new(big.Rat).SetFloat64(elapsedMinutes))
	f, _ := rat.Float64()
	if math.IsNaN(f) || math.IsInf(f, 0) {
		return 0
	}
	return f
}

func pollMiningBalance(ctx context.Context, store *miningRateStore, evmURL, address string, every time.Duration) {
	cl := &http.Client{Timeout: 15 * time.Second}
	ticker := time.NewTicker(every)
	defer ticker.Stop()
	for {
		wei, err := rpcEthGetBalance(cl, evmURL, address)
		if err != nil {
			store.observeBalance(false, nil, err.Error())
		} else {
			store.observeBalance(true, wei, "")
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

func rpcEthGetBalance(cl *http.Client, evmURL, address string) (*big.Int, error) {
	payload := fmt.Sprintf(`{"jsonrpc":"2.0","id":1,"method":"eth_getBalance","params":["%s","latest"]}`, address)
	req, err := http.NewRequestWithContext(context.Background(), http.MethodPost, evmURL, strings.NewReader(payload))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := cl.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	var wrap struct {
		Result json.RawMessage `json:"result"`
		Error  any             `json:"error"`
	}
	if err := json.Unmarshal(raw, &wrap); err != nil {
		return nil, err
	}
	if wrap.Error != nil {
		return nil, fmt.Errorf("%v", wrap.Error)
	}
	var hexStr string
	if err := json.Unmarshal(wrap.Result, &hexStr); err != nil {
		return nil, fmt.Errorf("eth_getBalance: unexpected result type")
	}
	hexv := strings.TrimSpace(hexStr)
	if hexv == "" || hexv == "0x" {
		return big.NewInt(0), nil
	}
	hexv = strings.TrimPrefix(strings.TrimPrefix(hexv, "0x"), "0X")
	n := new(big.Int)
	if _, ok := n.SetString(hexv, 16); !ok {
		return nil, fmt.Errorf("invalid eth_getBalance result")
	}
	return n, nil
}

type chartView struct {
	HasData       bool
	HeightRPCErr  string // BDAG JSON-RPC error (e.g. wrong rpcuser/rpcpass)
	Width         int
	Height        int
	PadX          int
	PadY          int
	InnerW        int
	InnerH        int
	PointsAttr    string
	Latest        float64
	AxisYMin      float64
	AxisYMax      float64
	LabelAxisX    int
	LabelAxisYMax int
	LabelAxisYMin int
	NumSamples    int
	PollInterval  string
	LabelY        int
	LabelRecentX  int
}

func buildChartView(samples []sample, pollSecs int, heightRPCErr string) chartView {
	const (
		W    = 640
		H    = 260
		padY = 28
		padX = 60
	)

	v := chartView{
		Width:        W,
		Height:       H,
		PadY:         padY,
		PadX:         padX,
		InnerW:       W - 2*padX,
		InnerH:       H - 2*padY,
		PollInterval: fmt.Sprintf("%ds", pollSecs),
		LabelY:       H - 6,
		LabelRecentX: W - padX,
		HeightRPCErr: strings.TrimSpace(heightRPCErr),
	}

	if v.HeightRPCErr != "" {
		v.HasData = false
		return v
	}

	if len(samples) < 2 {
		v.HasData = false
		return v
	}

	v.HasData = true
	v.NumSamples = len(samples)
	v.Latest = samples[len(samples)-1].Height

	minH := samples[0].Height
	maxH := samples[0].Height
	for _, p := range samples[1:] {
		if p.Height < minH {
			minH = p.Height
		}
		if p.Height > maxH {
			maxH = p.Height
		}
	}
	if maxH-minH < 1e-6 {
		minH -= 1
		maxH += 1
	}
	v.AxisYMin = minH
	v.AxisYMax = maxH
	v.LabelAxisX = padX - 6
	v.LabelAxisYMax = padY + 12
	v.LabelAxisYMin = padY + v.InnerH - 1

	var b strings.Builder
	nPts := len(samples)
	for i, p := range samples {
		t := float64(i) / float64(nPts-1)
		x := float64(padX) + t*float64(v.InnerW)
		norm := (p.Height - minH) / (maxH - minH)
		y := float64(H-2*padY)*(1-norm) + float64(padY)
		fmt.Fprintf(&b, "%.1f,%.1f ", x, y)
	}
	v.PointsAttr = strings.TrimSpace(b.String())
	return v
}
