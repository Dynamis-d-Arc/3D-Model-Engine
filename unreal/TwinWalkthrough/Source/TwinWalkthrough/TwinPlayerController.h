#pragma once

#include "CoreMinimal.h"
#include "GameFramework/PlayerController.h"
#include "TwinPlayerController.generated.h"

class ATwinCharacter;
class ATwinViewerDirector;

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
 */
UCLASS()
class TWINWALKTHROUGH_API ATwinPlayerController : public APlayerController
{
	GENERATED_BODY()

public:
	virtual void BeginPlay() override;
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

private:
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
	int32 TourFrames = 0;
	double TourSeconds = 0.0;
	bool bTourPushForward = false;
	bool bTourWaitingForLoad = false;
	TWeakObjectPtr<AActor> TourHvac;
};
