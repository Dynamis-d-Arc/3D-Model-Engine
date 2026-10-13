#pragma once

#include "CoreMinimal.h"
#include "GameFramework/PlayerController.h"
#include "TwinPlayerController.generated.h"

class ATwinCharacter;

/**
 * Walkthrough controls (polled each tick so no input assets are needed):
 *   WASD move | mouse / arrow keys look | Shift run | Space jump (walk) / up (fly) | Q/Ctrl down (fly)
 *   F fly on/off | G ghost (fly through walls) | C ceilings on/off | LMB select | RMB walk to point
 *   1-9 / Tab jump to room | X clear selection | H help | P screenshot | Esc quit
 *
 * -TwinTour [-TwinTourOut=<dir>] runs a scripted check (navigation, wall collision, selection,
 * screenshots per room) and writes tour_report.json, then exits.
 */
UCLASS()
class TWINWALKTHROUGH_API ATwinPlayerController : public APlayerController
{
	GENERATED_BODY()

public:
	virtual void BeginPlay() override;
	virtual void PlayerTick(float DeltaTime) override;

	// state read by the HUD
	TWeakObjectPtr<AActor> Selected;
	TWeakObjectPtr<AActor> CurrentRoomFloor;
	TWeakObjectPtr<AActor> ProjectInfo;
	TArray<TWeakObjectPtr<AActor>> RoomFloors;
	bool bShowHelp = true;
	bool bCeilingsVisible = true;
	bool bTour = false;
	FString Status;
	double StatusUntil = 0.0;

	FString ModeName() const;
	void SelectUnderCrosshair();
	void TeleportToRoom(int32 Index);
	void SetCeilingsVisible(bool bVisible);
	void Notify(const FString& Message, float Seconds = 2.5f);

private:
	ATwinCharacter* TwinPawn() const;
	void HandleInput(float DeltaTime);
	void UpdateCurrentRoom();
	void WalkToCrosshair();
	bool TraceFromCamera(FHitResult& Hit, float Distance) const;
	FVector RoomSpawn(const AActor* Floor) const;
	void PlaceCamera(const FVector& EyeLocation, const FVector& LookAt);

	// ---- tour -----------------------------------------------------------------------------------
	void StartTour();
	void TickTour(float DeltaTime);
	void Shot(const FString& Name);
	void FinishTour();

	TArray<TFunction<float()>> TourSteps;
	int32 TourIndex = 0;
	double TourNextAt = 0.0;
	FString TourOut;
	TSharedPtr<class FJsonObject> TourReport;
	TArray<FString> TourShots;
	FVector WalkTarget = FVector::ZeroVector;
	double WalkStarted = 0.0;
	FVector WallStart = FVector::ZeroVector;
	int32 TourFrames = 0;
	double TourSeconds = 0.0;
	bool bTourPushForward = false;
};
