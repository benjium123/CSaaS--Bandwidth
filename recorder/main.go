// lkrec joins LiveKit rooms and writes each participant's audio straight into
// its own .ogg file. Nothing is decoded or re-encoded: the files carry the exact
// Opus packets of the call, placed on one timeline that starts when recording
// started, so the agent file and the customer file line up.
//
// Consent: when /start names an announcement (an Ogg/Opus file under
// REC_DIR/announce), nothing is written until the phone side has answered and
// the announcement has been played into the room. If it cannot be played, the
// call is not recorded at all.
//
// Output per recording (REC_DIR):
//
//	<room>__<unix start>__<identity>.ogg   one file per participant
//	<room>__<unix start>.json              manifest, written when the recording ends
//
// On startup, .ogg files without a manifest (the recorder died mid-call) get a
// manifest marked "recovered", so the saved part of a call is never lost.
package main

import (
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/livekit/protocol/auth"
	"github.com/livekit/protocol/livekit"
	lksdk "github.com/livekit/server-sdk-go/v2"
	"github.com/pion/webrtc/v4"
	"github.com/pion/webrtc/v4/pkg/media/oggwriter"
)

var (
	roomNameRe     = regexp.MustCompile(`^[A-Za-z0-9_.-]{1,128}$`)
	announceNameRe = regexp.MustCompile(`^[A-Za-z0-9_.-]{1,128}\.ogg$`)
	errAlready     = errors.New("already recording")
	errUnknown     = errors.New("unknown room")
)

const announceTimeout = 60 * time.Second

type fileInfo struct {
	File          string `json:"file"`
	Identity      string `json:"identity"`
	Kind          string `json:"kind"` // "sip" = the phone side, otherwise the app user
	Bytes         int64  `json:"bytes"`
	DurationMs    int64  `json:"duration_ms"`
	Packets       int64  `json:"packets"`
	SilenceFrames int64  `json:"silence_frames"`
}

type manifest struct {
	Room          string     `json:"room"`
	StartedAt     time.Time  `json:"started_at"`
	EndedAt       time.Time  `json:"ended_at"`
	Announced     bool       `json:"announced"`
	AnnounceError string     `json:"announce_error,omitempty"`
	Recovered     bool       `json:"recovered,omitempty"`
	Files         []fileInfo `json:"files"`
}

type side struct {
	w    *sideWriter
	info fileInfo
}

type roomRec struct {
	name     string
	start    time.Time
	prefix   string // <room>__<unix start>
	dir      string
	announce string // absolute path of the announcement, "" = none
	room     *lksdk.Room
	wg       sync.WaitGroup
	armed    atomic.Bool // announcement done (or not required): packets may be written
	started  atomic.Bool // announcement playback claimed
	stopping atomic.Bool

	mu        sync.Mutex
	sides     map[string]*side
	announced bool
	annErr    string
}

// side returns the participant's side file, creating it on first use.
func (rr *roomRec) side(identity, kind string) (*sideWriter, error) {
	rr.mu.Lock()
	defer rr.mu.Unlock()
	if s, ok := rr.sides[identity]; ok {
		return s.w, nil
	}
	file := fmt.Sprintf("%s__%s.ogg", rr.prefix, sanitize(identity))
	w, err := oggwriter.New(filepath.Join(rr.dir, file), 48000, 2)
	if err != nil {
		return nil, err
	}
	s := &side{w: newSideWriter(w, rr.start), info: fileInfo{File: file, Identity: identity, Kind: kind}}
	rr.sides[identity] = s
	return s.w, nil
}

type recorder struct {
	mu        sync.Mutex
	rooms     map[string]*roomRec
	dir       string
	url       string
	apiKey    string
	apiSecret string
}

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func sanitize(s string) string {
	var b strings.Builder
	for _, r := range s {
		if b.Len() >= 64 {
			break
		}
		switch {
		case r == '+':
			b.WriteByte('p')
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9', r == '-', r == '.':
			b.WriteRune(r)
		default:
			b.WriteByte('-') // never '_': "__" separates the file name parts
		}
	}
	return b.String()
}

func kindName(k lksdk.ParticipantKind) string {
	switch k {
	case lksdk.ParticipantSIP:
		return "sip"
	case lksdk.ParticipantAgent:
		return "agent"
	default:
		return "standard"
	}
}

// token: the recorder joins as an AGENT-kind participant, like the P43 listener
// did, so apps treat it as a bot. It may publish only to play the announcement.
func (r *recorder) token(room string) (string, error) {
	yes, no := true, false
	at := auth.NewAccessToken(r.apiKey, r.apiSecret)
	at.SetVideoGrant(&auth.VideoGrant{
		RoomJoin: true, Room: room,
		CanSubscribe: &yes, CanPublish: &yes, CanPublishData: &no,
	}).SetIdentity("lkrec-" + room).SetName("Recorder").SetKind(livekit.ParticipantInfo_AGENT).SetValidFor(6 * time.Hour)
	return at.ToJWT()
}

func (r *recorder) start(name, announce string, resume bool) error {
	r.mu.Lock()
	if _, ok := r.rooms[name]; ok {
		r.mu.Unlock()
		return errAlready
	}
	now := time.Now()
	rr := &roomRec{
		name: name, start: now, dir: r.dir, sides: map[string]*side{},
		prefix: fmt.Sprintf("%s__%d", name, now.Unix()),
	}
	if announce != "" && !resume {
		rr.announce = filepath.Join(r.dir, "announce", announce)
	} else {
		rr.armed.Store(true) // no announcement required, or it already played before a restart
	}
	r.rooms[name] = rr // reserve the name before the slow connect
	r.mu.Unlock()

	cb := &lksdk.RoomCallback{
		ParticipantCallback: lksdk.ParticipantCallback{
			OnTrackSubscribed: func(t *webrtc.TrackRemote, pub *lksdk.RemoteTrackPublication, rp *lksdk.RemoteParticipant) {
				r.onTrack(rr, t, pub, rp)
				r.maybeAnnounce(rr, rp)
			},
			OnAttributesChanged: func(_ map[string]string, p lksdk.Participant) {
				r.maybeAnnounce(rr, p)
			},
		},
		OnParticipantDisconnected: func(rp *lksdk.RemoteParticipant) {
			if rp.Kind() == lksdk.ParticipantSIP || !hasPeople(rr.room) {
				go r.stop(name) // the phone side hung up, or only bots are left
			}
		},
		OnDisconnected: func() {
			go r.stop(name) // room closed by the server
		},
	}
	rr.room = lksdk.NewRoom(cb) // assigned before joining, so callbacks always see it
	tok, err := r.token(name)
	if err == nil {
		err = rr.room.JoinWithToken(r.url, tok, lksdk.WithAutoSubscribe(true))
	}
	if err != nil {
		r.mu.Lock()
		delete(r.rooms, name)
		r.mu.Unlock()
		return err
	}
	log.Printf("joined room=%s announce=%t", name, rr.announce != "")
	for _, rp := range rr.room.GetRemoteParticipants() { // the phone may have answered already
		r.maybeAnnounce(rr, rp)
	}
	return nil
}

// hasPeople reports whether anyone other than bots is still in the room.
func hasPeople(room *lksdk.Room) bool {
	for _, rp := range room.GetRemoteParticipants() {
		if rp.Kind() != lksdk.ParticipantAgent && rp.Kind() != lksdk.ParticipantEgress {
			return true
		}
	}
	return false
}

// maybeAnnounce plays the announcement once the phone side has answered
// (livekit-sip reports sip.callStatus; a missing status counts as answered),
// then arms the recording. A failed announcement leaves the call unrecorded.
func (r *recorder) maybeAnnounce(rr *roomRec, p lksdk.Participant) {
	if rr.announce == "" || p.Kind() != lksdk.ParticipantSIP {
		return
	}
	if st, ok := p.Attributes()["sip.callStatus"]; ok && st != "active" {
		return
	}
	if !rr.started.CompareAndSwap(false, true) {
		return
	}
	go func() {
		err := playFile(rr.room, rr.announce)
		rr.mu.Lock()
		if err != nil {
			rr.annErr = err.Error()
		} else {
			rr.announced = true
		}
		rr.mu.Unlock()
		if err != nil {
			log.Printf("announcement failed room=%s err=%v (call will not be recorded)", rr.name, err)
			return
		}
		rr.armed.Store(true)
		log.Printf("announced room=%s; recording", rr.name)
	}()
}

// playFile publishes an Ogg/Opus file as a microphone track, waits for it to
// finish, then unpublishes it. The packets are sent as they are: no encoding.
func playFile(room *lksdk.Room, path string) error {
	done := make(chan struct{})
	var once sync.Once
	track, err := lksdk.NewLocalFileTrack(path,
		lksdk.ReaderTrackWithFrameDuration(20*time.Millisecond),
		lksdk.ReaderTrackWithOnWriteComplete(func() { once.Do(func() { close(done) }) }),
	)
	if err != nil {
		return err
	}
	pub, err := room.LocalParticipant.PublishTrack(track, &lksdk.TrackPublicationOptions{
		Name: "monitor-announcement", Source: livekit.TrackSource_MICROPHONE,
	})
	if err != nil {
		return err
	}
	defer func() { _ = room.LocalParticipant.UnpublishTrack(pub.SID()) }()
	select {
	case <-done:
		time.Sleep(300 * time.Millisecond) // let the last packets play out
		return nil
	case <-time.After(announceTimeout):
		return errors.New("announcement did not finish in time")
	}
}

func (r *recorder) onTrack(rr *roomRec, t *webrtc.TrackRemote, pub *lksdk.RemoteTrackPublication, rp *lksdk.RemoteParticipant) {
	if t.Kind() != webrtc.RTPCodecTypeAudio || !strings.EqualFold(t.Codec().MimeType, webrtc.MimeTypeOpus) {
		log.Printf("skip track room=%s identity=%s kind=%s mime=%s", rr.name, rp.Identity(), t.Kind(), t.Codec().MimeType)
		return
	}
	if rr.stopping.Load() {
		return
	}
	log.Printf("track started room=%s identity=%s sid=%s", rr.name, rp.Identity(), pub.SID())
	rr.wg.Add(1)
	go func() {
		defer rr.wg.Done()
		recordTrack(t, rr, rp.Identity(), kindName(rp.Kind()))
	}()
}

func (r *recorder) stop(name string) (*manifest, error) {
	r.mu.Lock()
	rr, ok := r.rooms[name]
	if ok {
		delete(r.rooms, name)
	}
	r.mu.Unlock()
	if !ok {
		return nil, errUnknown
	}
	rr.stopping.Store(true)
	rr.room.Disconnect() // ends every ReadRTP loop
	done := make(chan struct{})
	go func() { rr.wg.Wait(); close(done) }()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		log.Printf("stop room=%s: track readers still closing after 5s", name)
	}

	rr.mu.Lock()
	m := &manifest{Room: name, StartedAt: rr.start, EndedAt: time.Now(), Announced: rr.announced, AnnounceError: rr.annErr}
	for _, s := range rr.sides {
		if err := s.w.close(); err != nil {
			log.Printf("close failed file=%s err=%v", s.info.File, err)
		}
		written, silence, _, ms := s.w.stats()
		fi := s.info
		fi.Packets, fi.SilenceFrames, fi.DurationMs = written, silence, ms
		if st, err := os.Stat(filepath.Join(rr.dir, fi.File)); err == nil {
			fi.Bytes = st.Size()
		}
		m.Files = append(m.Files, fi)
	}
	rr.mu.Unlock()
	sort.Slice(m.Files, func(i, j int) bool { return m.Files[i].File < m.Files[j].File })
	if err := writeManifest(rr.dir, rr.prefix, m); err != nil {
		log.Printf("manifest failed room=%s err=%v", name, err)
	}
	log.Printf("stopped room=%s files=%d announced=%t", name, len(m.Files), m.Announced)
	return m, nil
}

// writeManifest writes <prefix>.json atomically (temp file + rename), so a
// reader never sees half a manifest.
func writeManifest(dir, prefix string, m *manifest) error {
	data, err := json.MarshalIndent(m, "", "  ")
	if err != nil {
		return err
	}
	tmp := filepath.Join(dir, "."+prefix+".json.tmp")
	if err := os.WriteFile(tmp, data, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, filepath.Join(dir, prefix+".json"))
}

// recoverOrphans gives every recording left without a manifest (the recorder
// died mid-call) a manifest marked recovered. Ogg pages are written as they go,
// so the audio saved before the crash is playable.
func recoverOrphans(dir string) {
	files, _ := filepath.Glob(filepath.Join(dir, "*__*__*.ogg"))
	groups := map[string][]string{}
	for _, f := range files {
		base := filepath.Base(f)
		parts := strings.SplitN(strings.TrimSuffix(base, ".ogg"), "__", 3)
		if len(parts) != 3 {
			continue
		}
		prefix := parts[0] + "__" + parts[1]
		if _, err := os.Stat(filepath.Join(dir, prefix+".json")); err == nil {
			continue
		}
		groups[prefix] = append(groups[prefix], base)
	}
	for prefix, names := range groups {
		parts := strings.SplitN(prefix, "__", 2)
		secs, _ := strconv.ParseInt(parts[1], 10, 64)
		m := &manifest{Room: parts[0], StartedAt: time.Unix(secs, 0), EndedAt: time.Now(), Recovered: true}
		sort.Strings(names)
		for _, n := range names {
			fi := fileInfo{File: n, Identity: strings.TrimSuffix(strings.SplitN(n, "__", 3)[2], ".ogg"), Kind: "unknown"}
			if st, err := os.Stat(filepath.Join(dir, n)); err == nil {
				fi.Bytes = st.Size()
			}
			m.Files = append(m.Files, fi)
		}
		if err := writeManifest(dir, prefix, m); err != nil {
			log.Printf("recover failed prefix=%s err=%v", prefix, err)
			continue
		}
		log.Printf("recovered prefix=%s files=%d", prefix, len(names))
	}
}

// requireToken guards the API. The backend reaches the recorder over the docker
// bridge, so the port is not loopback-only; callers must send
// "Authorization: Bearer <hex sha256("lkrec:" + LIVEKIT_API_SECRET)>".
func requireToken(secret string, next http.Handler) http.Handler {
	sum := sha256.Sum256([]byte("lkrec:" + secret))
	want := []byte("Bearer " + hex.EncodeToString(sum[:]))
	return http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		if subtle.ConstantTimeCompare([]byte(req.Header.Get("Authorization")), want) != 1 {
			writeJSON(w, 401, map[string]any{"ok": false, "error": "unauthorized"})
			return
		}
		next.ServeHTTP(w, req)
	})
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

type startBody struct {
	Room         string `json:"room"`
	Announcement string `json:"announcement"` // file name under REC_DIR/announce, optional
	Resume       bool   `json:"resume"`       // re-joining after a recorder restart: do not announce again
}

func readBody(w http.ResponseWriter, req *http.Request, v *startBody) bool {
	if req.Method != http.MethodPost {
		writeJSON(w, 405, map[string]any{"ok": false, "error": "POST only"})
		return false
	}
	err := json.NewDecoder(http.MaxBytesReader(w, req.Body, 4096)).Decode(v)
	if err != nil || !roomNameRe.MatchString(v.Room) || (v.Announcement != "" && !announceNameRe.MatchString(v.Announcement)) {
		writeJSON(w, 400, map[string]any{"ok": false, "error": "bad request"})
		return false
	}
	return true
}

func main() {
	r := &recorder{
		rooms:     map[string]*roomRec{},
		dir:       env("REC_DIR", "/out"),
		url:       env("LIVEKIT_URL", "ws://127.0.0.1:7880"),
		apiKey:    os.Getenv("LIVEKIT_API_KEY"),
		apiSecret: os.Getenv("LIVEKIT_API_SECRET"),
	}
	if r.apiKey == "" || r.apiSecret == "" {
		log.Fatal("LIVEKIT_API_KEY and LIVEKIT_API_SECRET are required")
	}
	recoverOrphans(r.dir)

	mux := http.NewServeMux()
	mux.HandleFunc("/start", func(w http.ResponseWriter, req *http.Request) {
		var b startBody
		if !readBody(w, req, &b) {
			return
		}
		if b.Announcement != "" {
			if _, err := os.Stat(filepath.Join(r.dir, "announce", b.Announcement)); err != nil {
				writeJSON(w, 400, map[string]any{"ok": false, "error": "announcement file not found"})
				return
			}
		}
		switch err := r.start(b.Room, b.Announcement, b.Resume); {
		case errors.Is(err, errAlready):
			writeJSON(w, 200, map[string]any{"ok": true, "already": true}) // idempotent
		case err != nil:
			writeJSON(w, 502, map[string]any{"ok": false, "error": err.Error()})
		default:
			writeJSON(w, 200, map[string]any{"ok": true})
		}
	})
	mux.HandleFunc("/stop", func(w http.ResponseWriter, req *http.Request) {
		var b startBody
		if !readBody(w, req, &b) {
			return
		}
		m, err := r.stop(b.Room)
		if err != nil {
			writeJSON(w, 404, map[string]any{"ok": false, "error": err.Error()})
			return
		}
		writeJSON(w, 200, map[string]any{"ok": true, "manifest": m})
	})
	mux.HandleFunc("/health", func(w http.ResponseWriter, _ *http.Request) {
		r.mu.Lock()
		names := make([]string, 0, len(r.rooms))
		for n := range r.rooms {
			names = append(names, n)
		}
		r.mu.Unlock()
		sort.Strings(names)
		writeJSON(w, 200, map[string]any{"ok": true, "rooms": names})
	})

	srv := &http.Server{Addr: env("REC_LISTEN", "127.0.0.1:9099"), Handler: requireToken(r.apiSecret, mux), ReadHeaderTimeout: 5 * time.Second}
	go func() {
		sig := make(chan os.Signal, 1)
		signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
		<-sig
		r.mu.Lock()
		names := make([]string, 0, len(r.rooms))
		for n := range r.rooms {
			names = append(names, n)
		}
		r.mu.Unlock()
		for _, n := range names {
			_, _ = r.stop(n)
		}
		_ = srv.Close()
	}()
	log.Printf("lkrec listening on %s, writing to %s", srv.Addr, r.dir)
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatal(err)
	}
}
