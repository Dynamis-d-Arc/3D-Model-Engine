#pragma once

#include "CoreMinimal.h"
#include "GameFramework/PlayerController.h"
#include "TwinPlayerController.generated.h"

class ATwinCharacter;
class ATwinViewerDirector;
class FTwinMouseDeltaProcessor;

/**
 * Walkthrough controls (polled each tick so no input assets are needed):
 *   WASD move | mouse / arrow keys look | [ / ] mouse sensitivity | Shift run | Space jump (walk) / up (fly) | Q/Ctrl down (fly)
 *   F fly on/off | G ghost (fly through walls) | C ceilings on/off | LMB select | RMB walk to point
 *   1-9 / Tab jump to room | M room list | X clear selection | P screenshot | Esc quit
 *   H controls card (also shown by itself for HelpAutoSeconds after a building loads; otherwise the HUD shows a one-line
 *   hint bar) | I technical details in the HUD panels (model check numbers, data source, ids, object names)
 * Runtime viewer (a building loaded from the backend, see ATwinViewerDirector):
 *   V airflow illustration | B buildings (switch model) | R retry after an error | Ctrl+V paste a launch code
 *   PgUp / PgDn: next floor up / down in a multi-floor building (walking: the same spot when it is inside a room
 *   on that floor, else a stair or lift, else the floor's entrance; flying: the same view one storey up / down).
 *   With the ceilings hidden (C, or the airflow view) the floors above the current one are hidden too (cutaway).
 *
 * -TwinTour [-TwinTourOut=<dir>] runs a scripted check (navigation, wall collision, selection,
 * screenshots per room; in the viewer also HVAC selection + telemetry matching, the airflow layer and an
 * optional -TwinSwitchTo=<project>/<build> model switch) and writes tour_report.json, then exits
 * (exit code 3 when the building could not be loaded).
 *
 * Streaming mode (-TwinStreaming, or automatically with -PixelStreamingConnectionURL=...): the viewer runs on a
 * server and is shown in a browser through Pixel Streaming. Nothing of the server's desktop is used: Esc does not
 * quit (the browser uses it to free the mouse), no clipboard launch codes, no screenshots on the server. The
 * session ends by itself: -TwinIdleExit=<s> without input (default 600, exit code 4), -TwinMaxSession=<s>
 * (default 3600, exit code 5), or a load error that lasts 60 s (exit code 3). 0 disables a limit.
 */
UCLASS()
class TWINWALKTHROUGH_API ATwinPlayerController : public APlayerController
{
	GENERATED_BODY()

public:
	virtual void BeginPlay() override;
	virtual void EndPlay(const EEndPlayReason::Type Reason) override;
	virtual void PlayerTick(float DeltaTime) override;

	enum class EMenu : uint8 { None, Rooms, Buildings };

	struct FMenuItem
	{
		FBox2D Rect;
		int32 Index = INDEX_NONE;
	};

	// state read by the HUD
	TWeakObjectPtr<AActor> Selected;
	TWeakObjectPtr<AActor> CurrentRoomFloor;
	TWeakObjectPtr<AActor> ProjectInfo;
	TArray<TWeakObjectPtr<AActor>> RoomFloors;
	TWeakObjectPtr<ATwinViewerDirector> Director;
	bool bShowHelp = false;       // the full controls card (H)
	bool bShowDetails = false;    // technical details in the HUD panels (I)
	bool bCeilingsVisible = true;
	bool bTour = false;
	bool bStreaming = false;      // shown in a browser through Pixel Streaming (see the class comment)
	float LookSensitivity = 1.f;  // x BaseLookDegPerCount; [ / ] change it, -TwinLookSensitivity=<x> sets it
	FString Status;
	double StatusUntil = 0.0;
	EMenu Menu = EMenu::None;
	int32 MenuPage = 0;
	int32 CurrentFloor = 0;       // index into the director's Floors (multi-floor buildings)
	TArray<FMenuItem> MenuItems;  // laid out by the HUD each frame, hit-tested here on click

	FString ModeName() const;
	void SelectUnderCrosshair();
	void TeleportToRoom(int32 Index);
	void SetCeilingsVisible(bool bVisible);
	/** Next floor up (+1) or down (-1); returns how the landing place was chosen, or "" when there is none. */
	FString JumpFloor(int32 Direction);
	void Notify(const FString& Message, float Seconds = 2.5f);
	/** Streaming mode: seconds until the idle limit ends the session (< 0 = no limit). */
	double IdleSecondsLeft() const;
	/** Seconds until the controls card shown after a load closes by itself (< 0 = it is not counting down). */
	double HelpSecondsLeft() const;

private:
	/** Shows the controls card for HelpAutoSeconds (not in tours, not once the visitor has used H). */
	void ShowHelpForAWhile();
	static constexpr float HelpAutoSeconds = 10.f;
	double HelpAutoHideAt = 0.0;   // world time; 0 = no countdown
	bool bHelpToggled = false;     // the visitor pressed H: no more automatic cards in this session

	void TickStreamingSession();
	/** Mouse look this frame in mouse counts (+Y = up): streamed = deltas seen by the Slate pre-processor, desktop =
	 *  the raw mouse axes (the input axes add the engine's 0.07 axis scale, FOV scaling and mouse smoothing). */
	void LookDelta(float& MX, float& MY) const;
	/** Mouse + arrow-key look for this frame. */
	void TickLook(float DeltaTime);
	/** Turn the view (degrees; +pitch = up), pitch kept within +-85. */
	void ApplyLook(float YawDeg, float PitchDeg);
	/** [ / ] (or - / =): mouse sensitivity steps down / up; the desktop viewer keeps it for the next start. */
	void StepLookSensitivity(int32 Steps);
	static constexpr float BaseLookDegPerCount = 0.07f;   // 1.0x: ~5100 counts per full turn (16 cm at 800 DPI)
	TSharedPtr<FTwinMouseDeltaProcessor> MouseProcessor;   // streaming mode only
	FVector2D FrameMouse = FVector2D::ZeroVector;          // its deltas collected for the current frame
	float IdleExitSeconds = 0.f;
	float MaxSessionSeconds = 0.f;
	double LastInputAt = 0.0;
	double SessionStartedAt = 0.0;
	double ErrorSince = 0.0;
	bool bExitRequested = false;

	ATwinCharacter* TwinPawn() const;
	void HandleInput(float DeltaTime);
	void HandleMenuInput();
	void OpenMenu(EMenu Which);
	void CloseMenu();
	void PasteLaunch();
	void UpdateCurrentRoom();
	void SyncFloor();
	void ApplyFloorVisibility();
	int32 FloorOfActor(const AActor* A) const;
	void WalkToCrosshair();
	bool TraceFromCamera(FHitResult& Hit, float Distance) const;
	FVector RoomSpawn(const AActor* Floor) const;
	void PlaceCamera(const FVector& EyeLocation, const FVector& LookAt);
	void RefreshTwinActors();
	void OnTwinLoaded();
	void OnTwinUnloaded();
	void HoldPawn();
	void TakeScreenshot();

	// ---- tour -----------------------------------------------------------------------------------
	void StartTour();
	void AddViewerSteps();
	void AddFloorSteps();
	void TickTour(float DeltaTime);
	void Shot(const FString& Name);
	void FinishTour(int32 ExitCode = 0);

	TArray<TFunction<float()>> TourSteps;  // a step returning < 0 runs again after -value seconds
	int32 TourIndex = 0;
	double TourNextAt = 0.0;
	double StepStarted = 0.0;
	FString TourOut;
	TSharedPtr<class FJsonObject> TourReport;
	TArray<FString> TourShots;
	TSharedPtr<class FJsonObject> TourFloors;
	TArray<TSharedPtr<class FJsonValue>> TourJumps;
	FVector WalkTarget = FVector::ZeroVector;
	int32 WalkRoom = INDEX_NONE;
	int32 StartRoom = 0;
	int32 NavStableChecks = 0;
	double WalkStarted = 0.0;
	FVector WallStart = FVector::ZeroVector;
	FRotator LookStart = FRotator::ZeroRotator;
	int32 TourFrames = 0;
	double TourSeconds = 0.0;
	bool bTourPushForward = false;
	bool bTourWaitingForLoad = false;
	TWeakObjectPtr<AActor> TourHvac;
};
