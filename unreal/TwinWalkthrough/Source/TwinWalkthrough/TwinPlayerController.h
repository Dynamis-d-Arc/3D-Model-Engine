#pragma once

#include "CoreMinimal.h"
#include "GameFramework/PlayerController.h"
#include "TwinPlayerController.generated.h"

class ATwinCharacter;
class ATwinViewerDirector;
class FTwinMouseDeltaProcessor;

/**
 * Walkthrough controls (polled each tick so no input assets are needed):
 *   WASD move | mouse / arrow keys look | Shift run | Space jump (walk) / up (fly) | Q/Ctrl down (fly)
 *   F fly on/off | G ghost (fly through walls) | C ceilings on/off | LMB select | RMB walk to point
 *   1-9 / Tab jump to room | M room list | X clear selection | H help | P screenshot | Esc quit
 * Runtime viewer (a building loaded from the backend, see ATwinViewerDirector):
 *   V airflow illustration | B buildings (switch model) | R retry after an error | Ctrl+V paste a launch code
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
	bool bShowHelp = true;
	bool bCeilingsVisible = true;
	bool bTour = false;
	bool bStreaming = false;      // shown in a browser through Pixel Streaming (see the class comment)
	FString Status;
	double StatusUntil = 0.0;
	EMenu Menu = EMenu::None;
	int32 MenuPage = 0;
	TArray<FMenuItem> MenuItems;  // laid out by the HUD each frame, hit-tested here on click

	FString ModeName() const;
	void SelectUnderCrosshair();
	void TeleportToRoom(int32 Index);
	void SetCeilingsVisible(bool bVisible);
	void Notify(const FString& Message, float Seconds = 2.5f);
	/** Streaming mode: seconds until the idle limit ends the session (< 0 = no limit). */
	double IdleSecondsLeft() const;

private:
	void TickStreamingSession();
	/** Mouse look this frame: streamed = deltas seen by the Slate pre-processor, desktop = the input axes. */
	void LookDelta(float& MX, float& MY) const;
	/** Turn the view (degrees; +pitch = up), pitch kept within +-85. */
	void ApplyLook(float YawDeg, float PitchDeg);
	static constexpr float LookDegPerCount = 0.2f;   // mouse look sensitivity
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
