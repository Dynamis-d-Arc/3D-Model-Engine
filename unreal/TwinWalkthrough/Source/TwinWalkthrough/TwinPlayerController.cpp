#include "TwinPlayerController.h"

#include "Blueprint/AIBlueprintHelperLibrary.h"
#include "Camera/CameraComponent.h"
#include "Components/CapsuleComponent.h"
#include "Dom/JsonObject.h"
#include "Engine/GameViewportClient.h"
#include "Engine/LocalPlayer.h"
#include "Engine/World.h"
#include "EngineUtils.h"
#include "Engine/PointLight.h"
#include "Components/PointLightComponent.h"
#include "Framework/Application/IInputProcessor.h"
#include "Framework/Application/SlateApplication.h"
#include "GameFramework/CharacterMovementComponent.h"
#include "GameFramework/PlayerInput.h"
#include "GenericPlatform/GenericPlatformInputDeviceMapper.h"
#include "HAL/FileManager.h"
#include "HAL/PlatformApplicationMisc.h"
#include "InputKeyEventArgs.h"
#include "Kismet/KismetMathLibrary.h"
#include "Misc/CommandLine.h"
#include "Misc/ConfigCacheIni.h"
#include "Misc/FileHelper.h"
#include "Misc/Paths.h"
#include "NavigationData.h"
#include "NavigationPath.h"
#include "NavigationSystem.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"
#include "TwinAirflowActor.h"
#include "TwinCharacter.h"
#include "TwinMeta.h"
#include "TwinViewerDirector.h"
#include "UnrealClient.h"

/**
 * Streaming mode: Pixel Streaming hands browser mouse movement to Slate as raw mouse moves. Those reach the player
 * input axes (GetInputMouseDelta) only while the game viewport has captured the mouse, and an off-screen viewport
 * (-RenderOffscreen) has no window that could capture it. A pre-processor sees every mouse move before routing,
 * so the controller takes the look deltas from here instead.
 */
class FTwinMouseDeltaProcessor : public IInputProcessor
{
public:
	FVector2D Accumulated = FVector2D::ZeroVector;

	virtual void Tick(const float DeltaTime, FSlateApplication& SlateApp, TSharedRef<ICursor> Cursor) override {}
	virtual bool HandleMouseMoveEvent(FSlateApplication& SlateApp, const FPointerEvent& MouseEvent) override
	{
		Accumulated += MouseEvent.GetCursorDelta();
		return false;  // never consume: the menus still need the cursor
	}
	virtual const TCHAR* GetDebugName() const override { return TEXT("TwinMouseDelta"); }
};

namespace
{
	TArray<TSharedPtr<FJsonValue>> VecJson(const FVector& V)
	{
		return {MakeShared<FJsonValueNumber>(V.X), MakeShared<FJsonValueNumber>(V.Y), MakeShared<FJsonValueNumber>(V.Z)};
	}

	void SetNum(const TSharedPtr<FJsonObject>& O, const TCHAR* Key, float V)
	{
		if (!FMath::IsNaN(V))
		{
			O->SetNumberField(Key, V);
		}
	}

	// mouse sensitivity setting (GameUserSettings.ini of the desktop viewer)
	const TCHAR* LookIniSection = TEXT("TwinViewer");
	const TCHAR* LookIniKey = TEXT("LookSensitivity");
	constexpr float MinLookSensitivity = 0.2f;
	constexpr float MaxLookSensitivity = 5.f;
	constexpr float LookSensitivityStep = 1.25f;
}

void ATwinPlayerController::BeginPlay()
{
	Super::BeginPlay();
	Director = ATwinViewerDirector::Get(GetWorld());
	RefreshTwinActors();
	SetInputMode(FInputModeGameOnly());
	bShowMouseCursor = false;
	// Pixel Streaming: this viewer runs on a server and is shown in a browser (see the class comment)
	const TCHAR* Cmd = FCommandLine::Get();
	FString StreamUrl;
	bStreaming = FParse::Param(Cmd, TEXT("TwinStreaming")) || FParse::Value(Cmd, TEXT("PixelStreamingConnectionURL="), StreamUrl);
	if (bStreaming)
	{
		IdleExitSeconds = 600.f;
		MaxSessionSeconds = 3600.f;
		FParse::Value(Cmd, TEXT("TwinIdleExit="), IdleExitSeconds);
		FParse::Value(Cmd, TEXT("TwinMaxSession="), MaxSessionSeconds);
		if (FSlateApplication::IsInitialized())
		{
			MouseProcessor = MakeShared<FTwinMouseDeltaProcessor>();
			const bool bOk = FSlateApplication::Get().RegisterInputPreProcessor(MouseProcessor);
			UE_LOG(LogTemp, Log, TEXT("Twin: mouse look pre-processor %s"), bOk ? TEXT("registered") : TEXT("NOT registered"));
		}
		UE_LOG(LogTemp, Log, TEXT("Twin: streaming mode (idle exit %.0f s, max session %.0f s)"), IdleExitSeconds, MaxSessionSeconds);
	}
	LastInputAt = SessionStartedAt = FPlatformTime::Seconds();
	bTour = FParse::Param(FCommandLine::Get(), TEXT("TwinTour"));
	if (bTour)
	{
		FParse::Value(FCommandLine::Get(), TEXT("TwinTourOut="), TourOut);
		if (TourOut.IsEmpty())
		{
			TourOut = FPaths::ProjectSavedDir() / TEXT("TwinTour");
		}
		IFileManager::Get().MakeDirectory(*TourOut, true);
		bShowHelp = false;
	}
	// mouse sensitivity: -TwinLookSensitivity=<x>, else the one saved by [ / ] (desktop; tours stay at the default)
	if (!bStreaming && !bTour)
	{
		GConfig->GetFloat(LookIniSection, LookIniKey, LookSensitivity, GGameUserSettingsIni);
	}
	FParse::Value(Cmd, TEXT("TwinLookSensitivity="), LookSensitivity);
	LookSensitivity = FMath::Clamp(LookSensitivity, MinLookSensitivity, MaxLookSensitivity);
	if (ATwinViewerDirector* D = Director.Get())
	{
		// runtime viewer: actors arrive later; hold the visitor until the building exists
		D->OnLoaded.AddUObject(this, &ATwinPlayerController::OnTwinLoaded);
		D->OnUnloaded.AddUObject(this, &ATwinPlayerController::OnTwinUnloaded);
		bTourWaitingForLoad = bTour;
		if (D->IsReady())
		{
			OnTwinLoaded();
		}
		return;
	}
	if (bTour)
	{
		StartTour();
	}
	else
	{
		ShowHelpForAWhile();
		Notify(FString::Printf(TEXT("%d rooms loaded"), RoomFloors.Num()), 5.f);
	}
}

void ATwinPlayerController::RefreshTwinActors()
{
	RoomFloors.Reset();
	ProjectInfo.Reset();
	for (TActorIterator<AActor> It(GetWorld()); It; ++It)
	{
		AActor* A = *It;
		if (!IsValid(A) || A->IsActorBeingDestroyed())
		{
			continue;
		}
		if (TwinMeta::IsKind(A, TEXT("floor")))
		{
			RoomFloors.Add(A);
		}
		else if (TwinMeta::IsKind(A, TEXT("project")))
		{
			ProjectInfo = A;
		}
	}
	RoomFloors.Sort([](const TWeakObjectPtr<AActor>& A, const TWeakObjectPtr<AActor>& B)
	{
		return TwinMeta::Get(A.Get(), TEXT("Room")) < TwinMeta::Get(B.Get(), TEXT("Room"));
	});
}

void ATwinPlayerController::OnTwinLoaded()
{
	RefreshTwinActors();
	Selected.Reset();
	CurrentRoomFloor.Reset();
	bCeilingsVisible = true;
	CurrentFloor = 0;
	CloseMenu();
	ATwinViewerDirector* D = Director.Get();
	ATwinCharacter* C = TwinPawn();
	if (D && D->Manifest.IsValid() && C)
	{
		const TSharedPtr<FJsonObject> PS = D->Manifest->GetObjectField(TEXT("player_start"));
		const TArray<TSharedPtr<FJsonValue>>& L = PS->GetArrayField(TEXT("location_cm"));
		if (L.Num() == 3)
		{
			C->SetGhost(false);
			C->SetFlying(false);
			C->GetCharacterMovement()->StopMovementImmediately();
			C->SetActorLocation(FVector(L[0]->AsNumber(), L[1]->AsNumber(), L[2]->AsNumber() + ATwinCharacter::CapsuleHalfHeightCm + 5.0),
			                    false, nullptr, ETeleportType::TeleportPhysics);
			SetControlRotation(FRotator(0.f, (float)PS->GetNumberField(TEXT("yaw_deg")), 0.f));
		}
	}
	Notify(FString::Printf(TEXT("%s: %d rooms loaded"), D ? *D->Launch.ProjectName : TEXT("Building"), RoomFloors.Num()), 6.f);
	ShowHelpForAWhile();
	if (bTourWaitingForLoad)
	{
		bTourWaitingForLoad = false;
		StartTour();
	}
}

void ATwinPlayerController::OnTwinUnloaded()
{
	Selected.Reset();
	CurrentRoomFloor.Reset();
	RoomFloors.Reset();
	ProjectInfo.Reset();
	HoldPawn();
}

void ATwinPlayerController::HoldPawn()
{
	if (ATwinCharacter* C = TwinPawn())
	{
		// no building under the visitor yet: float in place instead of falling out of the world
		if (!C->IsGhost())
		{
			C->SetGhost(true);
		}
		C->GetCharacterMovement()->StopMovementImmediately();
	}
}

ATwinCharacter* ATwinPlayerController::TwinPawn() const
{
	return Cast<ATwinCharacter>(GetPawn());
}

void ATwinPlayerController::Notify(const FString& Message, float Seconds)
{
	Status = Message;
	StatusUntil = GetWorld()->GetTimeSeconds() + Seconds;
}

FString ATwinPlayerController::ModeName() const
{
	const ATwinCharacter* C = TwinPawn();
	if (!C)
	{
		return TEXT("-");
	}
	if (C->IsGhost())
	{
		return TEXT("Flying through walls");
	}
	return C->IsFlying() ? TEXT("Flying") : TEXT("Walking");
}

void ATwinPlayerController::ShowHelpForAWhile()
{
	if (bTour || bHelpToggled)
	{
		return;
	}
	bShowHelp = true;
	HelpAutoHideAt = GetWorld()->GetTimeSeconds() + HelpAutoSeconds;
}

double ATwinPlayerController::HelpSecondsLeft() const
{
	return HelpAutoHideAt > 0.0 ? FMath::Max(0.0, HelpAutoHideAt - GetWorld()->GetTimeSeconds()) : -1.0;
}

void ATwinPlayerController::EndPlay(const EEndPlayReason::Type Reason)
{
	if (MouseProcessor.IsValid() && FSlateApplication::IsInitialized())
	{
		FSlateApplication::Get().UnregisterInputPreProcessor(MouseProcessor);
	}
	MouseProcessor.Reset();
	Super::EndPlay(Reason);
}

void ATwinPlayerController::ApplyLook(float YawDeg, float PitchDeg)
{
	// Straight onto the control rotation. AddYawInput / AddPitchInput would be lost: the engine applies the pending
	// rotation input inside Super::PlayerTick and clears it when PlayerTick returns, and our input runs after Super.
	if (FMath::IsNearlyZero(YawDeg) && FMath::IsNearlyZero(PitchDeg))
	{
		return;
	}
	FRotator R = GetControlRotation();
	R.Yaw = FRotator::NormalizeAxis(R.Yaw + YawDeg);
	R.Pitch = FMath::ClampAngle(FRotator::NormalizeAxis(R.Pitch) + PitchDeg, -85.f, 85.f);
	R.Roll = 0.f;
	SetControlRotation(R);
}

void ATwinPlayerController::LookDelta(float& MX, float& MY) const
{
	if (MouseProcessor.IsValid())
	{
		MX = FrameMouse.X;
		MY = -FrameMouse.Y;  // Slate: +Y = down the screen; input axes: +Y = up
		return;
	}
	// raw counts: GetInputMouseDelta scales them by 0.07 (BaseInput.ini) and the FOV, and smooths them over frames,
	// which made the view slow and laggy to turn
	MX = PlayerInput ? PlayerInput->GetRawKeyValue(EKeys::MouseX) : 0.f;
	MY = PlayerInput ? PlayerInput->GetRawKeyValue(EKeys::MouseY) : 0.f;
}

void ATwinPlayerController::TickLook(float DeltaTime)
{
	float MX = 0.f, MY = 0.f;
	LookDelta(MX, MY);
	const float DegPerCount = BaseLookDegPerCount * LookSensitivity;
	const float KeyYaw = (IsInputKeyDown(EKeys::Right) ? 1.f : 0.f) - (IsInputKeyDown(EKeys::Left) ? 1.f : 0.f);
	const float KeyPitch = (IsInputKeyDown(EKeys::Up) ? 1.f : 0.f) - (IsInputKeyDown(EKeys::Down) ? 1.f : 0.f);
	ApplyLook(MX * DegPerCount + KeyYaw * 90.f * DeltaTime, MY * DegPerCount + KeyPitch * 60.f * DeltaTime);
}

void ATwinPlayerController::StepLookSensitivity(int32 Steps)
{
	LookSensitivity = FMath::Clamp(LookSensitivity * FMath::Pow(LookSensitivityStep, (float)Steps), MinLookSensitivity,
	                               MaxLookSensitivity);
	if (FMath::IsNearlyEqual(LookSensitivity, 1.f, 0.01f))
	{
		LookSensitivity = 1.f;
	}
	if (!bStreaming && !bTour)   // streamed: a shared server, so the setting lasts for this session only
	{
		GConfig->SetFloat(LookIniSection, LookIniKey, LookSensitivity, GGameUserSettingsIni);
		GConfig->Flush(false, GGameUserSettingsIni);
	}
	Notify(FString::Printf(TEXT("Mouse sensitivity %.2fx  ([ slower / ] faster)"), LookSensitivity));
}

double ATwinPlayerController::IdleSecondsLeft() const
{
	return (bStreaming && IdleExitSeconds > 0.f) ? IdleExitSeconds - (FPlatformTime::Seconds() - LastInputAt) : -1.0;
}

void ATwinPlayerController::TickStreamingSession()
{
	// a streamed session must end by itself: nobody is at the server to close it
	const double Now = FPlatformTime::Seconds();
	if (MouseProcessor.IsValid())   // this frame's mouse movement, taken once so menus do not pile it up
	{
		FrameMouse = MouseProcessor->Accumulated;
		MouseProcessor->Accumulated = FVector2D::ZeroVector;
	}
	float MX = 0.f, MY = 0.f;
	LookDelta(MX, MY);
	if (!FMath::IsNearlyZero(MX) || !FMath::IsNearlyZero(MY) || IsInputKeyDown(EKeys::AnyKey) ||
		WasInputKeyJustPressed(EKeys::AnyKey))
	{
		LastInputAt = Now;
	}
	const ATwinViewerDirector* D = Director.Get();
	if (D && D->State == ETwinLoadState::Error)
	{
		ErrorSince = ErrorSince > 0.0 ? ErrorSince : Now;
	}
	else
	{
		ErrorSince = 0.0;
	}
	int32 Code = 0;
	const TCHAR* Why = TEXT("");
	if (IdleExitSeconds > 0.f && Now - LastInputAt > IdleExitSeconds)
	{
		Code = 4;
		Why = TEXT("no input");
	}
	else if (MaxSessionSeconds > 0.f && Now - SessionStartedAt > MaxSessionSeconds)
	{
		Code = 5;
		Why = TEXT("maximum session length");
	}
	else if (ErrorSince > 0.0 && Now - ErrorSince > 60.0)
	{
		Code = 3;
		Why = TEXT("the building could not be loaded");
	}
	if (Code != 0 && !bExitRequested)
	{
		bExitRequested = true;
		UE_LOG(LogTemp, Log, TEXT("Twin: ending the streaming session (%s), exit code %d"), Why, Code);
		FPlatformMisc::RequestExitWithStatus(false, (uint8)Code);
	}
}

void ATwinPlayerController::PlayerTick(float DeltaTime)
{
	Super::PlayerTick(DeltaTime);
	if (bStreaming)
	{
		TickStreamingSession();
	}
	ATwinViewerDirector* D = Director.Get();
	if (D && !D->IsReady())
	{
		HoldPawn();
	}
	if (HelpAutoHideAt > 0.0 && GetWorld()->GetTimeSeconds() >= HelpAutoHideAt)
	{
		bShowHelp = false;   // the controls card shown after a load folds back into the hint bar
		HelpAutoHideAt = 0.0;
	}
	if (bTour)
	{
		TickTour(DeltaTime);
	}
	else if (Menu != EMenu::None)
	{
		HandleMenuInput();
	}
	else
	{
		HandleInput(DeltaTime);
	}
	UpdateCurrentRoom();
	SyncFloor();
}

void ATwinPlayerController::TakeScreenshot()
{
	const FString Dir = FPaths::ProjectSavedDir() / TEXT("Screenshots");
	IFileManager::Get().MakeDirectory(*Dir, true);
	const FString Path = FPaths::ConvertRelativePathToFull(Dir / FDateTime::Now().ToString(TEXT("twin_%Y%m%d_%H%M%S.png")));
	FScreenshotRequest::RequestScreenshot(Path, true, false);
	Notify(FString::Printf(TEXT("Screenshot saved: %s"), *Path), 4.f);
}

void ATwinPlayerController::PasteLaunch()
{
	FString Text;
	FPlatformApplicationMisc::ClipboardPaste(Text);
	FTwinLaunch L;
	FString Err;
	if (ATwinViewerDirector* D = Director.Get())
	{
		if (ATwinViewerDirector::ParseLaunch(Text, L, Err))
		{
			D->Open(L);
			Notify(TEXT("Opening the building from the pasted launch code"));
		}
		else
		{
			Notify(FString::Printf(TEXT("Clipboard: %s"), *Err), 5.f);
		}
	}
}

void ATwinPlayerController::OpenMenu(EMenu Which)
{
	Menu = Which;
	MenuPage = 0;
	MenuItems.Reset();
	bShowMouseCursor = true;
	SetInputMode(FInputModeGameAndUI().SetHideCursorDuringCapture(false));
	StopMovement();
	if (Which == EMenu::Buildings)
	{
		if (ATwinViewerDirector* D = Director.Get())
		{
			D->RefreshBuildings();
		}
	}
}

void ATwinPlayerController::CloseMenu()
{
	if (Menu == EMenu::None)
	{
		return;
	}
	Menu = EMenu::None;
	MenuItems.Reset();
	bShowMouseCursor = false;
	SetInputMode(FInputModeGameOnly());
}

void ATwinPlayerController::HandleMenuInput()
{
	if (WasInputKeyJustPressed(EKeys::Escape) || (Menu == EMenu::Rooms && WasInputKeyJustPressed(EKeys::M)) ||
		(Menu == EMenu::Buildings && WasInputKeyJustPressed(EKeys::B)))
	{
		CloseMenu();
		return;
	}
	if (WasInputKeyJustPressed(EKeys::PageDown) || WasInputKeyJustPressed(EKeys::MouseScrollDown) || WasInputKeyJustPressed(EKeys::Right))
	{
		++MenuPage;
	}
	if (WasInputKeyJustPressed(EKeys::PageUp) || WasInputKeyJustPressed(EKeys::MouseScrollUp) || WasInputKeyJustPressed(EKeys::Left))
	{
		MenuPage = FMath::Max(0, MenuPage - 1);
	}
	if (Menu == EMenu::Buildings && WasInputKeyJustPressed(EKeys::R))
	{
		if (ATwinViewerDirector* D = Director.Get())
		{
			D->RefreshBuildings();
		}
	}
	if (!WasInputKeyJustPressed(EKeys::LeftMouseButton))
	{
		return;
	}
	float MX = 0.f, MY = 0.f;
	if (!GetMousePosition(MX, MY))
	{
		return;
	}
	for (const FMenuItem& Item : MenuItems)
	{
		if (!Item.Rect.IsInside(FVector2D(MX, MY)))
		{
			continue;
		}
		const EMenu Was = Menu;
		CloseMenu();
		if (Was == EMenu::Rooms)
		{
			TeleportToRoom(Item.Index);
		}
		else if (ATwinViewerDirector* D = Director.Get())
		{
			if (D->Buildings.IsValidIndex(Item.Index))
			{
				const FTwinBuildingEntry& B = D->Buildings[Item.Index];
				D->OpenBuild(B.ProjectId, B.BuildId);
				Notify(FString::Printf(TEXT("Loading %s (v%d)"), *B.Name, B.Version), 4.f);
			}
		}
		return;
	}
}

void ATwinPlayerController::HandleInput(float DeltaTime)
{
	ATwinViewerDirector* D = Director.Get();
	const bool bCtrl = IsInputKeyDown(EKeys::LeftControl) || IsInputKeyDown(EKeys::RightControl);
	if (D)
	{
		if (!bStreaming && bCtrl && WasInputKeyJustPressed(EKeys::V))   // a streamed viewer has no clipboard of the user
		{
			PasteLaunch();
			return;
		}
		if (WasInputKeyJustPressed(EKeys::B))
		{
			OpenMenu(EMenu::Buildings);
			return;
		}
		if (D->State == ETwinLoadState::Error && WasInputKeyJustPressed(EKeys::R))
		{
			D->Retry();
		}
		if (!D->IsReady())
		{
			if (!bStreaming && WasInputKeyJustPressed(EKeys::Escape))
			{
				FPlatformMisc::RequestExit(false);  // works in Shipping too (no console commands there)
			}
			return;  // nothing to walk in yet
		}
		if (WasInputKeyJustPressed(EKeys::V))
		{
			if (!D->AirflowActor || D->AirflowActor->NumDots() == 0)
			{
				Notify(D->AirflowError.IsEmpty() ? TEXT("No airflow illustration for this building (no HVAC layer)") : D->AirflowError, 4.f);
			}
			else
			{
				D->SetAirflowVisible(!D->IsAirflowVisible());
				if (D->IsAirflowVisible() && bCeilingsVisible)
				{
					SetCeilingsVisible(false);  // the paths run just under the ceiling and above it to the units
				}
				Notify(D->IsAirflowVisible() ? TEXT("Airflow illustration on (schematic, not measured)") : TEXT("Airflow illustration off"));
			}
		}
		if (WasInputKeyJustPressed(EKeys::T) && D->IsAirflowVisible())
		{
			ATwinAirflowActor* A = D->AirflowActor;
			A->SetMistShown(!A->IsMistShown());
			Notify(A->IsMistShown() ? TEXT("Temperature mist on") : TEXT("Temperature mist off (tracers only)"));
		}
		if (WasInputKeyJustPressed(EKeys::PageUp))
		{
			JumpFloor(1);
		}
		if (WasInputKeyJustPressed(EKeys::PageDown))
		{
			JumpFloor(-1);
		}
	}
	ATwinCharacter* C = TwinPawn();
	if (!C)
	{
		return;
	}
	const float Fwd = (IsInputKeyDown(EKeys::W) ? 1.f : 0.f) - (IsInputKeyDown(EKeys::S) ? 1.f : 0.f);
	const float Right = (IsInputKeyDown(EKeys::D) ? 1.f : 0.f) - (IsInputKeyDown(EKeys::A) ? 1.f : 0.f);
	const float Up = ((IsInputKeyDown(EKeys::E) || IsInputKeyDown(EKeys::SpaceBar)) ? 1.f : 0.f) -
		((IsInputKeyDown(EKeys::Q) || IsInputKeyDown(EKeys::LeftControl)) ? 1.f : 0.f);
	const bool bRun = IsInputKeyDown(EKeys::LeftShift);
	C->GetCharacterMovement()->MaxWalkSpeed = bRun ? 500.f : 250.f;
	C->GetCharacterMovement()->MaxFlySpeed = bRun ? 900.f : 450.f;

	const FRotator Yaw(0.f, GetControlRotation().Yaw, 0.f);
	if (!FMath::IsNearlyZero(Fwd) || !FMath::IsNearlyZero(Right))
	{
		StopMovement();  // keyboard overrides click-to-walk
	}
	if (C->IsFlying())
	{
		C->AddMovementInput(GetControlRotation().Vector(), Fwd);
		C->AddMovementInput(FVector::UpVector, Up);
	}
	else
	{
		C->AddMovementInput(FRotationMatrix(Yaw).GetUnitAxis(EAxis::X), Fwd);
		if (WasInputKeyJustPressed(EKeys::SpaceBar))
		{
			C->Jump();
		}
	}
	C->AddMovementInput(FRotationMatrix(Yaw).GetUnitAxis(EAxis::Y), Right);

	TickLook(DeltaTime);
	if (WasInputKeyJustPressed(EKeys::LeftBracket) || WasInputKeyJustPressed(EKeys::Hyphen) ||
		WasInputKeyJustPressed(EKeys::Subtract))
	{
		StepLookSensitivity(-1);
	}
	if (WasInputKeyJustPressed(EKeys::RightBracket) || WasInputKeyJustPressed(EKeys::Equals) ||
		WasInputKeyJustPressed(EKeys::Add))
	{
		StepLookSensitivity(1);
	}

	if (WasInputKeyJustPressed(EKeys::F))
	{
		C->SetGhost(false);
		C->SetFlying(!C->IsFlying());
		Notify(FString::Printf(TEXT("Mode: %s"), *ModeName()));
	}
	if (WasInputKeyJustPressed(EKeys::G))
	{
		C->SetGhost(!C->IsGhost());
		if (!C->IsGhost())
		{
			C->SetFlying(false);
		}
		Notify(FString::Printf(TEXT("Mode: %s"), *ModeName()));
	}
	if (WasInputKeyJustPressed(EKeys::C))
	{
		SetCeilingsVisible(!bCeilingsVisible);
		Notify(bCeilingsVisible ? TEXT("Ceilings shown") : TEXT("Ceilings hidden"));
	}
	if (WasInputKeyJustPressed(EKeys::H))
	{
		bShowHelp = !bShowHelp;
		bHelpToggled = true;
		HelpAutoHideAt = 0.0;
	}
	if (WasInputKeyJustPressed(EKeys::I))
	{
		bShowDetails = !bShowDetails;
		Notify(bShowDetails ? TEXT("Technical details shown (I hides them)") : TEXT("Technical details hidden"));
	}
	if (WasInputKeyJustPressed(EKeys::X))
	{
		Selected.Reset();
	}
	if (WasInputKeyJustPressed(EKeys::M) && RoomFloors.Num() > 0)
	{
		OpenMenu(EMenu::Rooms);
		return;
	}
	if (WasInputKeyJustPressed(EKeys::LeftMouseButton))
	{
		SelectUnderCrosshair();
	}
	if (WasInputKeyJustPressed(EKeys::RightMouseButton))
	{
		WalkToCrosshair();
	}
	if (!bStreaming && WasInputKeyJustPressed(EKeys::P))   // streamed: the file would land on the server
	{
		TakeScreenshot();
	}
	if (!bStreaming && WasInputKeyJustPressed(EKeys::Escape))   // streamed: Esc frees the mouse in the browser
	{
		FPlatformMisc::RequestExit(false);  // works in Shipping too (no console commands there)
	}
	if (WasInputKeyJustPressed(EKeys::Tab) && RoomFloors.Num() > 0)
	{
		int32 Current = RoomFloors.IndexOfByKey(CurrentRoomFloor);
		TeleportToRoom((Current + 1) % RoomFloors.Num());
	}
	static const FKey Digits[] = {EKeys::One, EKeys::Two, EKeys::Three, EKeys::Four, EKeys::Five,
	                              EKeys::Six, EKeys::Seven, EKeys::Eight, EKeys::Nine};
	for (int32 i = 0; i < 9; ++i)
	{
		if (WasInputKeyJustPressed(Digits[i]))
		{
			TeleportToRoom(i);
		}
	}
}

bool ATwinPlayerController::TraceFromCamera(FHitResult& Hit, float Distance) const
{
	if (!PlayerCameraManager)
	{
		return false;
	}
	const FVector Start = PlayerCameraManager->GetCameraLocation();
	const FVector End = Start + PlayerCameraManager->GetCameraRotation().Vector() * Distance;
	// simple collision: architecture is complex-as-simple, furniture is its box (runtime and imported maps alike)
	FCollisionQueryParams Params(SCENE_QUERY_STAT(TwinSelect), false, GetPawn());
	return GetWorld()->LineTraceSingleByChannel(Hit, Start, End, ECC_Visibility, Params);
}

void ATwinPlayerController::SelectUnderCrosshair()
{
	FHitResult Hit;
	if (TraceFromCamera(Hit, 5000.f) && TwinMeta::IsTwin(Hit.GetActor()))
	{
		Selected = Hit.GetActor();
		const FString Label = TwinMeta::Get(Hit.GetActor(), TEXT("Label"));
		Notify(FString::Printf(TEXT("Selected %s"), *(Label.IsEmpty() ? Hit.GetActor()->GetActorNameOrLabel() : Label)));
	}
	else
	{
		Selected.Reset();
	}
}

void ATwinPlayerController::WalkToCrosshair()
{
	FHitResult Hit;
	if (!TraceFromCamera(Hit, 5000.f))
	{
		return;
	}
	ATwinCharacter* C = TwinPawn();
	if (C && C->IsFlying())
	{
		Notify(TEXT("Right click walks you there while walking (F stops flying)"));
		return;
	}
	UNavigationSystemV1* NavSys = FNavigationSystem::GetCurrent<UNavigationSystemV1>(GetWorld());
	if (NavSys && NavSys->IsNavigationBuildInProgress())
	{
		Notify(TEXT("The walking paths are still being prepared; try again in a moment"));
		return;
	}
	UAIBlueprintHelperLibrary::SimpleMoveToLocation(this, Hit.ImpactPoint);
	Notify(TEXT("Walking to target"));
}

void ATwinPlayerController::UpdateCurrentRoom()
{
	const APawn* P = GetPawn();
	if (!P)
	{
		return;
	}
	FHitResult Hit;
	FCollisionQueryParams Params(SCENE_QUERY_STAT(TwinRoom), true, P);
	const FVector Start = P->GetActorLocation();
	// trace down through anything that is not a floor (rugs, furniture)
	TArray<FHitResult> Hits;
	GetWorld()->LineTraceMultiByChannel(Hits, Start, Start - FVector(0, 0, 2000.f), ECC_Visibility, Params);
	AActor* Floor = nullptr;
	for (const FHitResult& H : Hits)
	{
		if (TwinMeta::IsKind(H.GetActor(), TEXT("floor")))
		{
			Floor = H.GetActor();
			break;
		}
	}
	if (!Floor && GetWorld()->LineTraceSingleByObjectType(Hit, Start, Start - FVector(0, 0, 2000.f),
	                                                       FCollisionObjectQueryParams(ECC_WorldStatic), Params))
	{
		Floor = TwinMeta::IsKind(Hit.GetActor(), TEXT("floor")) ? Hit.GetActor() : nullptr;
	}
	CurrentRoomFloor = Floor;
}

FVector ATwinPlayerController::RoomSpawn(const AActor* Floor) const
{
	FVector Spawn;
	if (TwinMeta::ParseVector(TwinMeta::Get(Floor, TEXT("Spawn")), Spawn))
	{
		return Spawn;
	}
	return Floor ? Floor->GetComponentsBoundingBox(true).GetCenter() : FVector::ZeroVector;
}

void ATwinPlayerController::TeleportToRoom(int32 Index)
{
	if (!RoomFloors.IsValidIndex(Index) || !RoomFloors[Index].IsValid() || !GetPawn())
	{
		return;
	}
	AActor* Floor = RoomFloors[Index].Get();
	StopMovement();
	const int32 RoomFloorIndex = FloorOfActor(Floor);
	if (RoomFloorIndex != INDEX_NONE && RoomFloorIndex != CurrentFloor)
	{
		CurrentFloor = RoomFloorIndex;
		ApplyFloorVisibility();
	}
	GetPawn()->SetActorLocation(RoomSpawn(Floor) + FVector(0, 0, ATwinCharacter::CapsuleHalfHeightCm + 5.f), false,
	                            nullptr, ETeleportType::TeleportPhysics);
	Notify(FString::Printf(TEXT("Room: %s"), *TwinMeta::Get(Floor, TEXT("RoomName"))));
}

void ATwinPlayerController::SetCeilingsVisible(bool bVisible)
{
	bCeilingsVisible = bVisible;
	for (TActorIterator<AActor> It(GetWorld()); It; ++It)
	{
		if (TwinMeta::IsKind(*It, TEXT("ceiling")))
		{
			It->SetActorHiddenInGame(!bVisible);
			It->SetActorEnableCollision(bVisible);  // hidden ceilings must not block selecting what is above them
		}
	}
	ApplyFloorVisibility();
}

int32 ATwinPlayerController::FloorOfActor(const AActor* A) const
{
	const ATwinViewerDirector* D = Director.Get();
	const FString Key = TwinMeta::Get(A, TEXT("Floor"));
	return (D && !Key.IsEmpty()) ? D->FloorIndexOf(Key) : INDEX_NONE;
}

void ATwinPlayerController::ApplyFloorVisibility()
{
	// cutaway: with the ceilings hidden the visitor wants to look into the current floor, so the floors above go too
	ATwinViewerDirector* D = Director.Get();
	if (!D || !D->IsMultiFloor())
	{
		return;
	}
	const bool bCut = !bCeilingsVisible;
	for (TActorIterator<AActor> It(GetWorld()); It; ++It)
	{
		const int32 F = FloorOfActor(*It);
		if (F == INDEX_NONE)
		{
			continue;
		}
		const bool bVisible = !(bCut && F > CurrentFloor) && (bCeilingsVisible || !TwinMeta::IsKind(*It, TEXT("ceiling")));
		if (It->IsHidden() == bVisible)
		{
			It->SetActorHiddenInGame(!bVisible);
			It->SetActorEnableCollision(bVisible);  // hidden floors must not block selecting what is below them
		}
	}
	for (ATwinAirflowActor* A : D->AirflowActors)
	{
		if (A)
		{
			A->SetFloorHidden(bCut && D->FloorIndexOf(A->FloorKey()) > CurrentFloor);
		}
	}
}

void ATwinPlayerController::SyncFloor()
{
	// walking: the floor under the visitor's feet (flying keeps the floor chosen with PgUp / PgDn)
	const ATwinCharacter* C = TwinPawn();
	if (!C || C->IsGhost() || C->IsFlying() || !CurrentRoomFloor.IsValid())
	{
		return;
	}
	const int32 F = FloorOfActor(CurrentRoomFloor.Get());
	if (F != INDEX_NONE && F != CurrentFloor)
	{
		CurrentFloor = F;
		ApplyFloorVisibility();
	}
}

FString ATwinPlayerController::JumpFloor(int32 Direction)
{
	ATwinViewerDirector* D = Director.Get();
	ATwinCharacter* C = TwinPawn();
	if (!D || !C)
	{
		return FString();
	}
	if (!D->IsMultiFloor())
	{
		Notify(TEXT("This building has one floor"));
		return FString();
	}
	const int32 From = FMath::Clamp(CurrentFloor, 0, D->Floors.Num() - 1);
	const int32 To = From + Direction;
	if (!D->Floors.IsValidIndex(To))
	{
		Notify(Direction > 0 ? TEXT("Already on the top floor") : TEXT("Already on the lowest floor"));
		return FString();
	}
	StopMovement();
	C->GetCharacterMovement()->StopMovementImmediately();
	const FVector Here = C->GetActorLocation();
	FString How;
	if (C->IsGhost() || C->IsFlying())
	{
		// the same view one storey up / down
		C->SetActorLocation(Here + FVector(0, 0, D->Floors[To].FloorZ - D->Floors[From].FloorZ), false, nullptr,
		                    ETeleportType::TeleportPhysics);
		How = TEXT("same view");
	}
	else
	{
		const FTwinFloorSpot Spot = D->FloorSpot(To, FVector2D(Here.X, Here.Y));
		const FVector Up(0, 0, ATwinCharacter::CapsuleHalfHeightCm + 5.f);
		FCollisionQueryParams Params(SCENE_QUERY_STAT(TwinFloorJump), false, C);
		const FCollisionShape Capsule = FCollisionShape::MakeCapsule(C->GetCapsuleComponent()->GetScaledCapsuleRadius(),
		                                                             ATwinCharacter::CapsuleHalfHeightCm);
		FVector Dest = Spot.Fallback + Up;
		How = Spot.Method == TEXT("same spot") ? FString(TEXT("room centre")) : Spot.Method;
		if (Spot.bSameValid && !GetWorld()->OverlapAnyTestByChannel(Spot.Same + Up, FQuat::Identity, ECC_Pawn, Capsule, Params))
		{
			Dest = Spot.Same + Up;
			How = TEXT("same spot");
		}
		C->SetActorLocation(Dest, false, nullptr, ETeleportType::TeleportPhysics);
	}
	CurrentFloor = To;
	ApplyFloorVisibility();
	Notify(FString::Printf(TEXT("%s (%d of %d)"), *D->Floors[To].Label, To + 1, D->Floors.Num()), 3.f);
	return How;
}

void ATwinPlayerController::PlaceCamera(const FVector& EyeLocation, const FVector& LookAt)
{
	if (APawn* P = GetPawn())
	{
		P->SetActorLocation(EyeLocation - FVector(0, 0, ATwinCharacter::EyeHeightCm - ATwinCharacter::CapsuleHalfHeightCm),
		                    false, nullptr, ETeleportType::TeleportPhysics);
	}
	SetControlRotation(UKismetMathLibrary::FindLookAtRotation(EyeLocation, LookAt));
}

// ------------------------------------------------------------------------------------------------
// scripted tour (automated verification)
// ------------------------------------------------------------------------------------------------

void ATwinPlayerController::Shot(const FString& Name)
{
	const FString Path = FPaths::ConvertRelativePathToFull(TourOut / (Name + TEXT(".png")));
	FScreenshotRequest::RequestScreenshot(Path, true, false);
	TourShots.Add(Name + TEXT(".png"));
}

void ATwinPlayerController::StartTour()
{
	TourReport = MakeShared<FJsonObject>();
	TourReport->SetStringField(TEXT("map"), GetWorld()->GetMapName());
	TourReport->SetStringField(TEXT("runtime"), Director.IsValid() ? TEXT("viewer (runtime glTF load)") : TEXT("imported map"));
#if UE_BUILD_SHIPPING
	TourReport->SetStringField(TEXT("build_configuration"), TEXT("Shipping"));
#elif UE_BUILD_DEVELOPMENT
	TourReport->SetStringField(TEXT("build_configuration"), TEXT("Development"));
#else
	TourReport->SetStringField(TEXT("build_configuration"), TEXT("Other"));
#endif
	TourReport->SetBoolField(TEXT("with_editor"), (bool)WITH_EDITOR);
	TourNextAt = GetWorld()->GetTimeSeconds() + 3.0;  // let streaming, Lumen and the nav mesh settle

	// 0) runtime viewer: load result + in-engine validation against the scene model
	if (ATwinViewerDirector* D = Director.Get())
	{
		TSharedPtr<FJsonObject> V = MakeShared<FJsonObject>();
		V->SetStringField(TEXT("project_id"), D->Launch.ProjectId);
		V->SetStringField(TEXT("build_id"), D->Launch.BuildId);
		V->SetStringField(TEXT("project_name"), D->Launch.ProjectName);
		V->SetStringField(TEXT("server"), D->Launch.Server);
		V->SetNumberField(TEXT("actors"), D->ActorsSpawned);
		V->SetNumberField(TEXT("triangles"), (double)D->TrianglesSpawned);
		V->SetNumberField(TEXT("model_bytes"), (double)D->BytesTotal);
		V->SetBoolField(TEXT("from_cache"), D->bFromCache);
		TSharedPtr<FJsonObject> T = MakeShared<FJsonObject>();
		for (const auto& Pair : D->Timings)
		{
			T->SetNumberField(Pair.Key, Pair.Value);
		}
		V->SetObjectField(TEXT("timings_s"), T);
		if (D->Validation.IsValid())
		{
			V->SetObjectField(TEXT("validation"), D->Validation);
		}
		TourReport->SetObjectField(TEXT("viewer"), V);
	}

	// 1) inventory of generated actors
	TourSteps.Add([this]() -> float
	{
		TMap<FString, int32> Kinds;
		int32 Total = 0;
		for (TActorIterator<AActor> It(GetWorld()); It; ++It)
		{
			if (TwinMeta::IsTwin(*It))
			{
				++Total;
				Kinds.FindOrAdd(TwinMeta::Get(*It, TEXT("Kind")))++;
			}
		}
		TSharedPtr<FJsonObject> ByKind = MakeShared<FJsonObject>();
		for (const auto& Pair : Kinds)
		{
			ByKind->SetNumberField(Pair.Key, Pair.Value);
		}
		TourReport->SetNumberField(TEXT("twin_actors"), Total);
		// room lights as spawned (lighting diagnosis)
		int32 Lights = 0, LightsHidden = 0;
		double Lumens = 0.0;
		TMap<FString, int32> PerFloor;
		for (TActorIterator<APointLight> It(GetWorld()); It; ++It)
		{
			++Lights;
			LightsHidden += It->IsHidden() ? 1 : 0;
			Lumens += It->PointLightComponent ? It->PointLightComponent->Intensity : 0.0;
			PerFloor.FindOrAdd(TwinMeta::Get(*It, TEXT("Floor")))++;
		}
		TSharedPtr<FJsonObject> LJ = MakeShared<FJsonObject>();
		LJ->SetNumberField(TEXT("point_lights"), Lights);
		LJ->SetNumberField(TEXT("hidden"), LightsHidden);
		LJ->SetNumberField(TEXT("lumens_total"), Lumens);
		for (const auto& Pair : PerFloor)
		{
			LJ->SetNumberField(Pair.Key.IsEmpty() ? TEXT("untagged") : *Pair.Key, Pair.Value);
		}
		TourReport->SetObjectField(TEXT("lights"), LJ);
		TourReport->SetObjectField(TEXT("by_kind"), ByKind);
		TArray<TSharedPtr<FJsonValue>> Rooms;
		for (const auto& F : RoomFloors)
		{
			TSharedPtr<FJsonObject> R = MakeShared<FJsonObject>();
			R->SetStringField(TEXT("id"), TwinMeta::Get(F.Get(), TEXT("Room")));
			R->SetStringField(TEXT("name"), TwinMeta::Get(F.Get(), TEXT("RoomName")));
			R->SetStringField(TEXT("dims"), TwinMeta::Get(F.Get(), TEXT("Dims")));
			R->SetArrayField(TEXT("spawn_cm"), VecJson(RoomSpawn(F.Get())));
			Rooms.Add(MakeShared<FJsonValueObject>(R));
		}
		TourReport->SetArrayField(TEXT("rooms"), Rooms);
		if (const APawn* P = GetPawn())
		{
			TourReport->SetArrayField(TEXT("player_start_cm"), VecJson(P->GetActorLocation()));
		}
		StepStarted = GetWorld()->GetTimeSeconds();
		return 0.5f;
	});

	// 1b) wait for the navigation mesh (runtime-loaded geometry is navigated once its tiles are generated),
	// then start from the first room whose spawn point is on it
	TourSteps.Add([this]() -> float
	{
		UNavigationSystemV1* NavSys = FNavigationSystem::GetCurrent<UNavigationSystemV1>(GetWorld());
		const double Waited = GetWorld()->GetTimeSeconds() - StepStarted;
		StartRoom = INDEX_NONE;
		int32 OnNav = 0;
		for (int32 i = 0; NavSys && i < RoomFloors.Num(); ++i)
		{
			FNavLocation L;
			if (RoomFloors[i].IsValid() && NavSys->ProjectPointToNavigation(RoomSpawn(RoomFloors[i].Get()) + FVector(0, 0, 50), L, FVector(60, 60, 200)))
			{
				++OnNav;
				StartRoom = StartRoom == INDEX_NONE ? i : StartRoom;
			}
		}
		const bool bBusy = NavSys && NavSys->IsNavigationBuildInProgress();
		NavStableChecks = (!bBusy && OnNav > 0) ? NavStableChecks + 1 : 0;
		if (NavSys && NavStableChecks < 3 && Waited < 120.0)
		{
			return -0.5f;
		}
		StartRoom = FMath::Max(StartRoom, 0);
		// runtime viewer: start where a visitor enters (the manifest's player start room) when it is on the nav mesh
		if (ATwinViewerDirector* D = Director.Get())
		{
			FString PsRoom;
			if (D->Manifest.IsValid() && D->Manifest->GetObjectField(TEXT("player_start"))->TryGetStringField(TEXT("room_id"), PsRoom))
			{
				for (int32 i = 0; NavSys && i < RoomFloors.Num(); ++i)
				{
					FNavLocation L;
					if (RoomFloors[i].IsValid() && TwinMeta::Get(RoomFloors[i].Get(), TEXT("Room")) == PsRoom &&
						NavSys->ProjectPointToNavigation(RoomSpawn(RoomFloors[i].Get()) + FVector(0, 0, 50), L, FVector(60, 60, 200)))
					{
						StartRoom = i;
					}
				}
			}
		}
		TourReport->SetNumberField(TEXT("nav_build_wait_s"), Waited);
		TourReport->SetNumberField(TEXT("rooms_on_navmesh"), OnNav);
		return 0.5f;
	});

	// 2) navigation: path between the first and last room (+ reachability of every room)
	TourSteps.Add([this]() -> float
	{
		TSharedPtr<FJsonObject> Nav = MakeShared<FJsonObject>();
		UNavigationSystemV1* NavSys = FNavigationSystem::GetCurrent<UNavigationSystemV1>(GetWorld());
		Nav->SetBoolField(TEXT("system"), NavSys != nullptr);
		// floors are not connected (no stairs to walk): the walk and the reachability stay on the start room's floor
		const int32 StartFloor = RoomFloors.IsValidIndex(StartRoom) ? FloorOfActor(RoomFloors[StartRoom].Get()) : INDEX_NONE;
		auto SameFloor = [this, StartFloor](int32 i) { return FloorOfActor(RoomFloors[i].Get()) == StartFloor; };
		WalkRoom = INDEX_NONE;
		for (int32 i = RoomFloors.Num() - 1; i >= 0 && WalkRoom == INDEX_NONE; --i)
		{
			WalkRoom = (i != StartRoom && SameFloor(i)) ? i : INDEX_NONE;
		}
		WalkRoom = WalkRoom == INDEX_NONE ? StartRoom : WalkRoom;
		if (NavSys && RoomFloors.Num() >= 2)
		{
			const FVector A = RoomSpawn(RoomFloors[StartRoom].Get()) + FVector(0, 0, 50);
			const FVector B = RoomSpawn(RoomFloors[WalkRoom].Get()) + FVector(0, 0, 50);
			// diagnostics: the default nav data and a raw query between the projected points
			if (ANavigationData* ND = NavSys->GetDefaultNavDataInstance(FNavigationSystem::DontCreate))
			{
				FNavLocation PA, PB;
				const bool bPA = NavSys->ProjectPointToNavigation(A, PA, FVector(60, 60, 200), ND);
				const bool bPB = NavSys->ProjectPointToNavigation(B, PB, FVector(60, 60, 200), ND);
				const FPathFindingQuery Q(this, *ND, bPA ? PA.Location : A, bPB ? PB.Location : B);
				const FPathFindingResult Res = NavSys->FindPathSync(Q);
				Nav->SetStringField(TEXT("nav_data"), ND->GetName());
				Nav->SetBoolField(TEXT("start_on_navmesh"), bPA);
				Nav->SetArrayField(TEXT("start_projected_cm"), VecJson(PA.Location));
				Nav->SetNumberField(TEXT("raw_query_result"), (int32)Res.Result);
				Nav->SetBoolField(TEXT("raw_query_partial"), Res.Path.IsValid() && Res.Path->IsPartial());
				Nav->SetNumberField(TEXT("raw_query_points"), Res.Path.IsValid() ? Res.Path->GetPathPoints().Num() : 0);
				Nav->SetArrayField(TEXT("query_extent"), VecJson(ND->GetDefaultQueryExtent()));
			}
			UNavigationPath* Path = NavSys->FindPathToLocationSynchronously(GetWorld(), A, B);
			const bool bValid = Path && Path->IsValid() && !Path->IsPartial();
			Nav->SetStringField(TEXT("from"), TwinMeta::Get(RoomFloors[StartRoom].Get(), TEXT("RoomName")));
			Nav->SetStringField(TEXT("to"), TwinMeta::Get(RoomFloors[WalkRoom].Get(), TEXT("RoomName")));
			Nav->SetBoolField(TEXT("valid"), bValid);
			Nav->SetNumberField(TEXT("points"), Path ? Path->PathPoints.Num() : 0);
			Nav->SetNumberField(TEXT("length_cm"), Path ? Path->GetPathLength() : 0.0);
			Nav->SetNumberField(TEXT("straight_cm"), FVector::Dist2D(A, B));
			if (Path)
			{
				TArray<TSharedPtr<FJsonValue>> Pts;
				for (const FVector& P : Path->PathPoints)
				{
					Pts.Add(MakeShared<FJsonValueArray>(VecJson(P)));
				}
				Nav->SetArrayField(TEXT("points_cm"), Pts);
			}
			// reachability of every room from the start room (listed first); the walk goes to the last
			// room, or to the farthest reachable one when the plan leaves the last room without a door
			TArray<TSharedPtr<FJsonValue>> Reach;
			double Farthest = -1.0;
			int32 FarIndex = INDEX_NONE, Reachable = 0;
			TArray<int32> Order = {StartRoom};
			int32 OtherFloors = 0;
			for (int32 i = 0; i < RoomFloors.Num(); ++i)
			{
				if (i != StartRoom && SameFloor(i))
				{
					Order.Add(i);
				}
				OtherFloors += SameFloor(i) ? 0 : 1;
			}
			Nav->SetNumberField(TEXT("rooms_on_other_floors"), OtherFloors);
			for (const int32 i : Order)
			{
				const FVector T = RoomSpawn(RoomFloors[i].Get()) + FVector(0, 0, 50);
				UNavigationPath* P = NavSys->FindPathToLocationSynchronously(GetWorld(), A, T);
				const bool bReach = P && P->IsValid() && !P->IsPartial();
				TSharedPtr<FJsonObject> R = MakeShared<FJsonObject>();
				R->SetStringField(TEXT("room"), TwinMeta::Get(RoomFloors[i].Get(), TEXT("RoomName")));
				R->SetBoolField(TEXT("reachable"), bReach);
				R->SetNumberField(TEXT("length_cm"), P ? P->GetPathLength() : -1.0);
				if (P && P->PathPoints.Num())
				{
					R->SetArrayField(TEXT("end_cm"), VecJson(P->PathPoints.Last()));
				}
				Reach.Add(MakeShared<FJsonValueObject>(R));
				if (bReach && i != StartRoom)
				{
					++Reachable;
					if (P->GetPathLength() > Farthest)
					{
						Farthest = P->GetPathLength();
						FarIndex = i;
					}
				}
			}
			Nav->SetArrayField(TEXT("reachability"), Reach);
			Nav->SetNumberField(TEXT("rooms_reachable"), Reachable);
			if (!bValid && FarIndex != INDEX_NONE)
			{
				WalkRoom = FarIndex;
			}
			Nav->SetStringField(TEXT("walk_to"), TwinMeta::Get(RoomFloors[WalkRoom].Get(), TEXT("RoomName")));
		}
		TourReport->SetObjectField(TEXT("navigation"), Nav);
		return 0.1f;
	});

	// 3) click-to-walk: path-follow from the first room to the target room through the doors
	TourSteps.Add([this]() -> float
	{
		if (RoomFloors.Num() < 2 || !RoomFloors.IsValidIndex(WalkRoom))
		{
			return 0.1f;
		}
		TeleportToRoom(StartRoom);
		WalkTarget = RoomSpawn(RoomFloors[WalkRoom].Get());
		WalkStarted = GetWorld()->GetTimeSeconds();
		UAIBlueprintHelperLibrary::SimpleMoveToLocation(this, WalkTarget);
		return 1.0f;
	});
	TourSteps.Add([this]() -> float
	{
		// keep walking until arrival (up to 60 s for large floors)
		if (RoomFloors.Num() >= 2 && GetPawn() && FVector::Dist2D(GetPawn()->GetActorLocation(), WalkTarget) > 60.f &&
			GetWorld()->GetTimeSeconds() - WalkStarted < 60.0)
		{
			return -0.5f;
		}
		return 0.1f;
	});
	TourSteps.Add([this]() -> float
	{
		if (RoomFloors.Num() < 2 || !GetPawn() || !RoomFloors.IsValidIndex(WalkRoom))
		{
			return 0.1f;
		}
		const float Remaining = FVector::Dist2D(GetPawn()->GetActorLocation(), WalkTarget);
		TSharedPtr<FJsonObject> Walk = MakeShared<FJsonObject>();
		Walk->SetStringField(TEXT("to"), TwinMeta::Get(RoomFloors[WalkRoom].Get(), TEXT("RoomName")));
		Walk->SetNumberField(TEXT("remaining_cm"), Remaining);
		Walk->SetBoolField(TEXT("arrived"), Remaining < 80.f);
		Walk->SetNumberField(TEXT("seconds"), GetWorld()->GetTimeSeconds() - WalkStarted);
		Walk->SetStringField(TEXT("ended_in_room"), TwinMeta::Get(CurrentRoomFloor.Get(), TEXT("RoomName")));
		TourReport->SetObjectField(TEXT("walk"), Walk);
		StopMovement();
		return 0.2f;
	});

	// 4) collision: walk west into the wall from the start room's centre for 3 s
	TourSteps.Add([this]() -> float
	{
		TeleportToRoom(StartRoom);
		SetControlRotation(FRotator(0.f, 180.f, 0.f));
		WallStart = GetPawn() ? GetPawn()->GetActorLocation() : FVector::ZeroVector;
		bTourPushForward = true;
		return 3.0f;
	});
	TourSteps.Add([this]() -> float
	{
		bTourPushForward = false;
		TSharedPtr<FJsonObject> Wall = MakeShared<FJsonObject>();
		const AActor* Floor = RoomFloors.IsValidIndex(StartRoom) ? RoomFloors[StartRoom].Get() : nullptr;
		const FVector End = GetPawn() ? GetPawn()->GetActorLocation() : FVector::ZeroVector;
		const FBox Bounds = Floor ? Floor->GetComponentsBoundingBox(true) : FBox(ForceInit);
		Wall->SetStringField(TEXT("room"), TwinMeta::Get(Floor, TEXT("RoomName")));
		Wall->SetArrayField(TEXT("start_cm"), VecJson(WallStart));
		Wall->SetArrayField(TEXT("end_cm"), VecJson(End));
		Wall->SetNumberField(TEXT("moved_cm"), FVector::Dist2D(WallStart, End));
		Wall->SetBoolField(TEXT("stayed_inside_room"), Bounds.IsValid && End.X > Bounds.Min.X && End.X < Bounds.Max.X &&
		                   End.Y > Bounds.Min.Y && End.Y < Bounds.Max.Y);
		TourReport->SetObjectField(TEXT("wall_collision"), Wall);
		return 0.2f;
	});

	// 4b) look: the same turn the mouse / arrow keys make must still be there a few frames later
	TourSteps.Add([this]() -> float
	{
		SetControlRotation(FRotator(0.f, 180.f, 0.f));
		LookStart = GetControlRotation();
		ApplyLook(30.f, 10.f);
		return 0.3f;
	});
	TourSteps.Add([this]() -> float
	{
		const FRotator Now = GetControlRotation();
		const float Yaw = FRotator::NormalizeAxis(Now.Yaw - LookStart.Yaw);
		const float Pitch = FRotator::NormalizeAxis(Now.Pitch - LookStart.Pitch);
		TSharedPtr<FJsonObject> Look = MakeShared<FJsonObject>();
		Look->SetNumberField(TEXT("yaw_turned_deg"), Yaw);
		Look->SetNumberField(TEXT("pitch_turned_deg"), Pitch);
		Look->SetBoolField(TEXT("ok"), FMath::IsNearlyEqual(Yaw, 30.f, 0.5f) && FMath::IsNearlyEqual(Pitch, 10.f, 0.5f));
		TourReport->SetObjectField(TEXT("look"), Look);
		return 0.2f;
	});

	// 4c) mouse look sensitivity: 100 counts right + 50 up, fed in where real mouse moves arrive (the player input
	// stack on the desktop, the Slate pre-processor when streamed), turn the view by counts x deg/count
	TourSteps.Add([this]() -> float
	{
		SetControlRotation(FRotator(0.f, 180.f, 0.f));
		LookStart = GetControlRotation();
		if (MouseProcessor.IsValid())
		{
			MouseProcessor->Accumulated += FVector2D(100.f, -50.f);   // Slate: +Y = down
		}
		else
		{
			FViewport* Viewport = GetLocalPlayer() && GetLocalPlayer()->ViewportClient ? GetLocalPlayer()->ViewportClient->Viewport : nullptr;
			const FInputDeviceId Device = IPlatformInputDeviceMapper::Get().GetDefaultInputDevice();
			const uint64 Now = FPlatformTime::Cycles64();
			InputKey(FInputKeyEventArgs(Viewport, Device, EKeys::MouseX, 100.f, 1.f / 60.f, 1, Now));
			InputKey(FInputKeyEventArgs(Viewport, Device, EKeys::MouseY, 50.f, 1.f / 60.f, 1, Now));
		}
		return 0.f;   // handed over on the next frame
	});
	TourSteps.Add([this]() -> float
	{
		TickLook(0.f);
		const FRotator Now = GetControlRotation();
		const float Yaw = FRotator::NormalizeAxis(Now.Yaw - LookStart.Yaw);
		const float Pitch = FRotator::NormalizeAxis(Now.Pitch - LookStart.Pitch);
		const float DegPerCount = BaseLookDegPerCount * LookSensitivity;
		TSharedPtr<FJsonObject> Mouse = MakeShared<FJsonObject>();
		Mouse->SetNumberField(TEXT("sensitivity"), LookSensitivity);
		Mouse->SetNumberField(TEXT("deg_per_count"), DegPerCount);
		Mouse->SetNumberField(TEXT("counts_per_turn"), 360.f / DegPerCount);
		Mouse->SetNumberField(TEXT("yaw_turned_deg"), Yaw);
		Mouse->SetNumberField(TEXT("pitch_turned_deg"), Pitch);
		Mouse->SetBoolField(TEXT("ok"), FMath::IsNearlyEqual(Yaw, 100.f * DegPerCount, 0.05f) &&
		                    FMath::IsNearlyEqual(Pitch, 50.f * DegPerCount, 0.05f));
		TourReport->SetObjectField(TEXT("mouse_look"), Mouse);
		return 0.2f;
	});

	// 5) one eye-level screenshot per room (from a corner, looking across the room); -TwinTourRooms=N limits them
	int32 ShotRooms = RoomFloors.Num();
	FParse::Value(FCommandLine::Get(), TEXT("TwinTourRooms="), ShotRooms);
	for (int32 i = 0; i < FMath::Min(ShotRooms, RoomFloors.Num()); ++i)
	{
		TourSteps.Add([this, i]() -> float
		{
			if (!RoomFloors.IsValidIndex(i) || !RoomFloors[i].IsValid())
			{
				return 0.f;
			}
			const AActor* Floor = RoomFloors[i].Get();
			const FBox B = Floor->GetComponentsBoundingBox(true);
			const FVector Spawn = RoomSpawn(Floor);
			const FVector Corner(B.Min.X + 70.f, B.Max.Y - 70.f, Spawn.Z + ATwinCharacter::EyeHeightCm);
			PlaceCamera(Corner, FVector(B.GetCenter().X, B.GetCenter().Y, Spawn.Z + 100.f));
			return 2.0f;
		});
		TourSteps.Add([this, i]() -> float
		{
			if (RoomFloors.IsValidIndex(i) && RoomFloors[i].IsValid())
			{
				Shot(FString::Printf(TEXT("room_%s"), *TwinMeta::Get(RoomFloors[i].Get(), TEXT("Room"))));
			}
			return 0.4f;
		});
	}

	// 5b) the full controls card (H) over the last view; every other shot shows the one-line hint bar
	TourSteps.Add([this]() -> float { bShowHelp = true; return 0.6f; });
	TourSteps.Add([this]() -> float { Shot(TEXT("help")); return 0.4f; });
	TourSteps.Add([this]() -> float { bShowHelp = false; return 0.1f; });

	// 6) select a furniture item and show its info panel
	TourSteps.Add([this]() -> float
	{
		AActor* Target = nullptr;
		float Best = 0.f;
		for (TActorIterator<AActor> It(GetWorld()); It; ++It)
		{
			if (TwinMeta::IsKind(*It, TEXT("furniture")) && TwinMeta::Get(*It, TEXT("Asset")) != TEXT("rug"))
			{
				const float Size = It->GetComponentsBoundingBox(true).GetSize().Size();
				if (Size > Best)
				{
					Best = Size;
					Target = *It;
				}
			}
		}
		if (Target)
		{
			const FBox B = Target->GetComponentsBoundingBox(true);
			const AActor* Floor = nullptr;
			for (const auto& F : RoomFloors)
			{
				if (TwinMeta::Get(F.Get(), TEXT("Room")) == TwinMeta::Get(Target, TEXT("Room")))
				{
					Floor = F.Get();
				}
			}
			const FVector Spawn = Floor ? RoomSpawn(Floor) : B.GetCenter();
			FVector Dir = (Spawn - B.GetCenter()).GetSafeNormal2D();
			if (Dir.IsNearlyZero())
			{
				Dir = FVector(1, 0, 0);
			}
			const FVector Eye = B.GetCenter() + Dir * 230.f + FVector(0, 0, 60.f);
			PlaceCamera(FVector(Eye.X, Eye.Y, Spawn.Z + ATwinCharacter::EyeHeightCm), B.GetCenter());
		}
		return 1.5f;
	});
	TourSteps.Add([this]() -> float
	{
		SelectUnderCrosshair();
		TSharedPtr<FJsonObject> Sel = MakeShared<FJsonObject>();
		const AActor* A = Selected.Get();
		Sel->SetBoolField(TEXT("selected"), A != nullptr);
		if (A)
		{
			for (const TCHAR* Key : {TEXT("Name"), TEXT("Kind"), TEXT("Label"), TEXT("Asset"), TEXT("Size"), TEXT("RoomName"), TEXT("Id")})
			{
				Sel->SetStringField(FString(Key).ToLower(), TwinMeta::Get(A, Key));
			}
		}
		TourReport->SetObjectField(TEXT("selection"), Sel);
		return 0.6f;
	});
	TourSteps.Add([this]() -> float { Shot(TEXT("selection")); return 0.5f; });

	if (Director.IsValid())
	{
		AddViewerSteps();
		AddFloorSteps();
	}

	// 7) overview from above with ceilings hidden (ghost mode)
	TourSteps.Add([this]() -> float
	{
		Selected.Reset();
		if (ATwinCharacter* C = TwinPawn())
		{
			C->SetGhost(true);
		}
		CurrentFloor = Director.IsValid() ? FMath::Max(0, Director->Floors.Num() - 1) : 0;
		SetCeilingsVisible(false);
		FBox All(ForceInit);
		for (const auto& F : RoomFloors)
		{
			if (F.IsValid())
			{
				All += F->GetComponentsBoundingBox(true);
			}
		}
		const FVector Center = All.GetCenter();
		const float Span = FMath::Max(All.GetSize().X, All.GetSize().Y);
		PlaceCamera(Center + FVector(-0.45f * Span, 0.75f * Span, 0.95f * Span), Center);
		return 2.5f;
	});
	TourSteps.Add([this]() -> float { Shot(TEXT("overview")); return 1.0f; });
#if !UE_BUILD_SHIPPING
	// same view with the navigation mesh drawn (green = walkable)
	TourSteps.Add([this]() -> float { ConsoleCommand(TEXT("show Navigation")); return 1.0f; });
	TourSteps.Add([this]() -> float { Shot(TEXT("navmesh")); return 1.0f; });
	TourSteps.Add([this]() -> float { ConsoleCommand(TEXT("show Navigation")); return 0.5f; });
#endif

	// 8) runtime viewer: switch to another building in the same session (-TwinSwitchTo=<project>/<build>)
	FString SwitchTo;
	if (Director.IsValid() && FParse::Value(FCommandLine::Get(), TEXT("TwinSwitchTo="), SwitchTo) && SwitchTo.Contains(TEXT("/")))
	{
		TourSteps.Add([this, SwitchTo]() -> float
		{
			FString Pid, Bid;
			SwitchTo.Split(TEXT("/"), &Pid, &Bid);
			StepStarted = GetWorld()->GetTimeSeconds();
			bTourWaitingForLoad = false;
			Director->OpenBuild(Pid, Bid);
			return 1.0f;
		});
		TourSteps.Add([this]() -> float
		{
			ATwinViewerDirector* D = Director.Get();
			if (D && D->IsBusy() && GetWorld()->GetTimeSeconds() - StepStarted < 300.0)
			{
				return -0.5f;
			}
			TSharedPtr<FJsonObject> S = MakeShared<FJsonObject>();
			S->SetStringField(TEXT("project_id"), D ? D->Launch.ProjectId : FString());
			S->SetStringField(TEXT("build_id"), D ? D->Launch.BuildId : FString());
			S->SetStringField(TEXT("project_name"), D ? D->Launch.ProjectName : FString());
			S->SetBoolField(TEXT("ready"), D && D->IsReady());
			S->SetStringField(TEXT("error_code"), D ? D->ErrorCode : FString());
			S->SetStringField(TEXT("error"), D ? D->ErrorMessage : FString());
			S->SetNumberField(TEXT("seconds"), GetWorld()->GetTimeSeconds() - StepStarted);
			if (D && D->IsReady())
			{
				int32 Twin = 0;
				for (TActorIterator<AActor> It(GetWorld()); It; ++It)
				{
					Twin += TwinMeta::IsTwin(*It) && !It->IsActorBeingDestroyed() ? 1 : 0;
				}
				S->SetNumberField(TEXT("actors"), D->ActorsSpawned);
				S->SetNumberField(TEXT("twin_actors_in_world"), Twin);
				S->SetNumberField(TEXT("rooms"), RoomFloors.Num());
				S->SetNumberField(TEXT("load_count"), D->LoadCount);
				if (D->Validation.IsValid())
				{
					S->SetObjectField(TEXT("validation"), D->Validation);
				}
			}
			TourReport->SetObjectField(TEXT("switch"), S);
			if (ATwinCharacter* C = TwinPawn())
			{
				C->SetGhost(true);
			}
			SetCeilingsVisible(false);
			FBox All(ForceInit);
			for (const auto& F : RoomFloors)
			{
				if (F.IsValid())
				{
					All += F->GetComponentsBoundingBox(true);
				}
			}
			if (All.IsValid)
			{
				const float Span = FMath::Max(All.GetSize().X, All.GetSize().Y);
				PlaceCamera(All.GetCenter() + FVector(-0.45f * Span, 0.75f * Span, 0.95f * Span), All.GetCenter());
			}
			return 2.5f;
		});
		TourSteps.Add([this]() -> float { Shot(TEXT("switched_overview")); return 1.0f; });
	}
	TourSteps.Add([this]() -> float { FinishTour(); return 2.0f; });
	TourSteps.Add([this]() -> float { FGenericPlatformMisc::RequestExit(false); return 10.f; });
}

void ATwinPlayerController::AddViewerSteps()
{
	// HVAC: hide the ceilings, look up at a unit, select it and record identity + live reading
	TourSteps.Add([this]() -> float
	{
		// the unit serving the largest room (a clear view of its diffusers and of the airflow layer)
		AActor* Unit = nullptr;
		double BestArea = -1.0;
		for (TActorIterator<AActor> It(GetWorld()); It; ++It)
		{
			if (!TwinMeta::IsKind(*It, TEXT("hvac_unit")))
			{
				continue;
			}
			double Area = 0.0;
			for (const auto& F : RoomFloors)
			{
				if (F.IsValid() && TwinMeta::Get(F.Get(), TEXT("Room")) == TwinMeta::Get(*It, TEXT("Room")))
				{
					const FVector Sz = F->GetComponentsBoundingBox(true).GetSize();
					Area = Sz.X * Sz.Y;
				}
			}
			if (Area > BestArea)
			{
				BestArea = Area;
				Unit = *It;
			}
		}
		TourHvac = Unit;
		if (!Unit)
		{
			return 0.1f;
		}
		CurrentFloor = FMath::Max(0, FloorOfActor(Unit));  // multi-floor: the unit's floor (the cutaway hides the ones above)
		SetCeilingsVisible(false);
		if (ATwinCharacter* Ch = TwinPawn())
		{
			Ch->SetGhost(true);  // hold the camera exactly where it is placed
		}
		const FBox B = Unit->GetComponentsBoundingBox(true);
		const FVector C = B.GetCenter();
		float EyeZ = B.Min.Z - 150.f;
		for (const auto& F : RoomFloors)
		{
			if (F.IsValid() && TwinMeta::Get(F.Get(), TEXT("Room")) == TwinMeta::Get(Unit, TEXT("Room")))
			{
				EyeZ = RoomSpawn(F.Get()).Z + ATwinCharacter::EyeHeightCm;
			}
		}
		PlaceCamera(FVector(C.X - 80.f, C.Y + 30.f, EyeZ), C);
		return 2.0f;
	});
	TourSteps.Add([this]() -> float
	{
		ATwinViewerDirector* D = Director.Get();
		TSharedPtr<FJsonObject> H = MakeShared<FJsonObject>();
		if (!TourHvac.IsValid() || !D)
		{
			H->SetBoolField(TEXT("has_hvac"), false);
			TourReport->SetObjectField(TEXT("hvac"), H);
			return 0.1f;
		}
		H->SetBoolField(TEXT("has_hvac"), true);
		SelectUnderCrosshair();
		const AActor* Sel = Selected.Get();
		H->SetBoolField(TEXT("selected"), Sel == TourHvac.Get());
		H->SetStringField(TEXT("selected_kind"), TwinMeta::Get(Sel, TEXT("Kind")));
		H->SetStringField(TEXT("selected_tag"), TwinMeta::Get(Sel, TEXT("Tag")));
		H->SetStringField(TEXT("selected_unit"), TwinMeta::Get(Sel, TEXT("Unit")));
		H->SetStringField(TEXT("selected_label"), TwinMeta::Get(Sel, TEXT("Label")));
		// telemetry identity matching: every HVAC unit actor against the readings it is shown with
		const FTwinTelemetry& T = D->Telemetry;
		H->SetStringField(TEXT("telemetry_source"), T.Source);
		H->SetStringField(TEXT("telemetry_label"), T.Label);
		H->SetBoolField(TEXT("telemetry_measured"), T.bMeasured);
		H->SetNumberField(TEXT("telemetry_units"), T.Units.Num());
		H->SetNumberField(TEXT("build_units"), T.BuildUnits);
		H->SetNumberField(TEXT("server_missing"), T.Missing);
		H->SetNumberField(TEXT("server_unknown"), T.Unknown);
		H->SetStringField(TEXT("telemetry_error"), T.Error);
		int32 UnitActors = 0, Matched = 0;
		TArray<TSharedPtr<FJsonValue>> Mismatch;
		for (TActorIterator<AActor> It(GetWorld()); It; ++It)
		{
			if (!TwinMeta::IsKind(*It, TEXT("hvac_unit")))
			{
				continue;
			}
			++UnitActors;
			const FTwinHvacReading* R = D->ReadingFor(TwinMeta::Get(*It, TEXT("Unit")));
			if (R && R->Tag.Equals(TwinMeta::Get(*It, TEXT("Tag")), ESearchCase::IgnoreCase))
			{
				++Matched;
			}
			else if (Mismatch.Num() < 20)
			{
				Mismatch.Add(MakeShared<FJsonValueString>(TwinMeta::Get(*It, TEXT("Tag"))));
			}
		}
		H->SetNumberField(TEXT("unit_actors"), UnitActors);
		H->SetNumberField(TEXT("unit_actors_matched"), Matched);
		H->SetArrayField(TEXT("unmatched_tags"), Mismatch);
		if (Sel)
		{
			if (const FTwinHvacReading* R = D->ReadingFor(TwinMeta::Get(Sel, TEXT("Unit"))))
			{
				TSharedPtr<FJsonObject> Rd = MakeShared<FJsonObject>();
				Rd->SetStringField(TEXT("tag"), R->Tag);
				Rd->SetBoolField(TEXT("on"), R->bOn);
				Rd->SetStringField(TEXT("mode"), R->Mode);
				SetNum(Rd, TEXT("setpoint_c"), R->SetpointC);
				SetNum(Rd, TEXT("supply_air_c"), R->SupplyC);
				SetNum(Rd, TEXT("return_air_c"), R->ReturnC);
				SetNum(Rd, TEXT("fan_speed"), R->Fan);
				SetNum(Rd, TEXT("airflow_lps"), R->AirflowLps);
				Rd->SetStringField(TEXT("alarm"), R->Alarm);
				H->SetObjectField(TEXT("selected_reading"), Rd);
			}
		}
		TourReport->SetObjectField(TEXT("hvac"), H);
		return 0.6f;
	});
	TourSteps.Add([this]() -> float
	{
		if (TourHvac.IsValid())
		{
			Shot(TEXT("hvac_selection"));
		}
		return 0.5f;
	});
	// the same with the technical details (I): data source, ids, object name, model check numbers
	TourSteps.Add([this]() -> float { bShowDetails = TourHvac.IsValid(); return 0.4f; });
	TourSteps.Add([this]() -> float
	{
		if (TourHvac.IsValid())
		{
			Shot(TEXT("hvac_details"));
		}
		return 0.4f;
	});
	TourSteps.Add([this]() -> float { bShowDetails = false; return 0.1f; });

	// airflow illustration: switch it on, look across the unit's room, record what is drawn and how it is labelled
	TourSteps.Add([this]() -> float
	{
		ATwinViewerDirector* D = Director.Get();
		if (!D || !TourHvac.IsValid())
		{
			return 0.1f;
		}
		CurrentFloor = FMath::Max(0, FloorOfActor(TourHvac.Get()));
		ApplyFloorVisibility();
		D->SetAirflowVisible(true);
		Selected.Reset();
		const FVector U = TourHvac->GetComponentsBoundingBox(true).GetCenter();
		FVector Eye(U.X - 300.f, U.Y, 160.f);
		for (const auto& F : RoomFloors)
		{
			if (F.IsValid() && TwinMeta::Get(F.Get(), TEXT("Room")) == TwinMeta::Get(TourHvac.Get(), TEXT("Room")))
			{
				Eye = RoomSpawn(F.Get()) + FVector(0, 0, ATwinCharacter::EyeHeightCm);  // free floor inside the room
			}
		}
		// standing in the unit's room, looking up towards its diffusers and the air falling into the room
		PlaceCamera(Eye, FVector(U.X, U.Y, Eye.Z + 40.f));
		return 2.5f;
	});
	TourSteps.Add([this]() -> float
	{
		ATwinViewerDirector* D = Director.Get();
		TSharedPtr<FJsonObject> A = MakeShared<FJsonObject>();
		A->SetBoolField(TEXT("visible"), D && D->IsAirflowVisible());
		if (D && D->AirflowActors.Num())
		{
			// summed over the floors' illustrations (one actor per floor with HVAC in a multi-floor building)
			int32 Paths = 0, Dots = 0, Moving = 0, Units = 0, Zones = 0, Shown = 0, MaskW = 0, MaskH = 0;
			bool bMaterial = true, bParams = true;
			float Lo = TNumericLimits<float>::Max(), Hi = -TNumericLimits<float>::Max();
			TArray<TSharedPtr<FJsonValue>> PerFloor;
			for (ATwinAirflowActor* Af : D->AirflowActors)
			{
				if (!Af)
				{
					continue;
				}
				Paths += Af->NumPaths();
				Dots += Af->NumDots();
				Moving += Af->VisibleDots();
				Units += Af->NumUnits();
				Zones += Af->NumMistZones();
				Shown += Af->VisibleMistZones();
				MaskW = FMath::Max(MaskW, Af->MaskSize().X);
				MaskH = FMath::Max(MaskH, Af->MaskSize().Y);
				bMaterial &= Af->HasMistMaterial();
				bParams &= Af->MistParamsOk();
				const FVector2D Range = Af->TemperatureRange();
				if (!FMath::IsNaN(Range.X))
				{
					Lo = FMath::Min(Lo, (float)Range.X);
					Hi = FMath::Max(Hi, (float)Range.Y);
				}
				TSharedPtr<FJsonObject> F = MakeShared<FJsonObject>();
				F->SetStringField(TEXT("floor"), Af->FloorKey());
				F->SetNumberField(TEXT("units"), Af->NumUnits());
				F->SetNumberField(TEXT("mist_visible"), Af->VisibleMistZones());
				F->SetStringField(TEXT("driver"), Af->Driver());
				F->SetArrayField(TEXT("offset_cm"), VecJson(Af->GetActorLocation()));
				PerFloor.Add(MakeShared<FJsonValueObject>(F));
			}
			A->SetNumberField(TEXT("paths"), Paths);
			A->SetNumberField(TEXT("dots"), Dots);
			A->SetNumberField(TEXT("dots_moving"), Moving);
			A->SetNumberField(TEXT("units"), Units);
			A->SetArrayField(TEXT("floors"), PerFloor);
			TSharedPtr<FJsonObject> M = MakeShared<FJsonObject>();
			M->SetBoolField(TEXT("material"), bMaterial);
			M->SetBoolField(TEXT("params_ok"), bParams);
			M->SetNumberField(TEXT("zones"), Zones);
			M->SetNumberField(TEXT("visible"), Shown);
			M->SetStringField(TEXT("driver"), D->AirflowActor ? D->AirflowActor->Driver() : FString());
			M->SetNumberField(TEXT("mask_w"), MaskW);
			M->SetNumberField(TEXT("mask_h"), MaskH);
			if (Lo <= Hi)
			{
				M->SetNumberField(TEXT("temp_min_c"), Lo);
				M->SetNumberField(TEXT("temp_max_c"), Hi);
			}
			A->SetObjectField(TEXT("mist"), M);
		}
		A->SetStringField(TEXT("error"), D ? D->AirflowError : FString());
		if (D && D->Airflow.IsValid())
		{
			A->SetStringField(TEXT("kind"), D->Airflow->GetStringField(TEXT("kind")));
			A->SetStringField(TEXT("disclaimer"), D->Airflow->GetStringField(TEXT("disclaimer")));
			const TSharedPtr<FJsonObject>* Th = nullptr;
			if (D->Airflow->TryGetObjectField(TEXT("thermal"), Th))
			{
				A->SetObjectField(TEXT("thermal"), *Th);
			}
		}
		TourReport->SetObjectField(TEXT("airflow"), A);
		Shot(TEXT("airflow"));
		return 0.8f;
	});
	// the same unit from above and to the side, ceilings hidden (the web Live tab's view)
	TourSteps.Add([this]() -> float
	{
		if (!TourHvac.IsValid())
		{
			return 0.1f;
		}
		if (ATwinCharacter* C = TwinPawn())
		{
			C->SetGhost(true);
		}
		CurrentFloor = FMath::Max(0, FloorOfActor(TourHvac.Get()));
		SetCeilingsVisible(false);
		FBox B = TourHvac->GetComponentsBoundingBox(true).ExpandBy(400.f);
		for (const auto& F : RoomFloors)
		{
			if (F.IsValid() && TwinMeta::Get(F.Get(), TEXT("Room")) == TwinMeta::Get(TourHvac.Get(), TEXT("Room")))
			{
				B = F->GetComponentsBoundingBox(true);
			}
		}
		const FVector C(B.GetCenter().X, B.GetCenter().Y, 100.f);
		const float Span = FMath::Max(B.GetSize().X, B.GetSize().Y);
		PlaceCamera(C + FVector(-0.5f * Span, 0.4f * Span, 0.45f * Span + 300.f), C);
		return 2.0f;
	});
	TourSteps.Add([this]() -> float { Shot(TEXT("airflow_above")); return 0.8f; });
	// the whole building from above with the mist on
	TourSteps.Add([this]() -> float
	{
		CurrentFloor = Director.IsValid() ? FMath::Max(0, Director->Floors.Num() - 1) : 0;
		ApplyFloorVisibility();
		FBox All(ForceInit);
		for (const auto& F : RoomFloors)
		{
			if (F.IsValid())
			{
				All += F->GetComponentsBoundingBox(true);
			}
		}
		if (!All.IsValid)
		{
			return 0.1f;
		}
		const FVector Center = All.GetCenter();
		const float Span = FMath::Max(All.GetSize().X, All.GetSize().Y);
		PlaceCamera(Center + FVector(-0.45f * Span, 0.75f * Span, 0.95f * Span), Center);
		return 2.0f;
	});
	TourSteps.Add([this]() -> float { Shot(TEXT("airflow_overview")); return 0.8f; });
	TourSteps.Add([this]() -> float
	{
		if (ATwinViewerDirector* D = Director.Get())
		{
			D->SetAirflowVisible(false);
		}
		SetCeilingsVisible(true);
		return 0.2f;
	});
}

void ATwinPlayerController::TickTour(float DeltaTime)
{
	if (!TourReport.IsValid())
	{
		// runtime viewer: waiting for the first load; a failed load ends the run with exit code 3
		ATwinViewerDirector* D = Director.Get();
		if (D && D->State == ETwinLoadState::Error)
		{
			TourReport = MakeShared<FJsonObject>();
			TourReport->SetStringField(TEXT("runtime"), TEXT("viewer (runtime glTF load)"));
			TSharedPtr<FJsonObject> E = MakeShared<FJsonObject>();
			E->SetStringField(TEXT("code"), D->ErrorCode);
			E->SetStringField(TEXT("message"), D->ErrorMessage);
			E->SetStringField(TEXT("server"), D->Launch.Server);
			TourReport->SetObjectField(TEXT("load_error"), E);
			Shot(TEXT("load_error"));
			TourSteps.Reset();
			TourIndex = 0;
			TourSteps.Add([this]() -> float { FinishTour(3); return 10.f; });
			TourNextAt = GetWorld()->GetTimeSeconds() + 1.0;
		}
		else if (D && D->State == ETwinLoadState::Idle && GetWorld()->GetTimeSeconds() > 5.0)
		{
			TourReport = MakeShared<FJsonObject>();
			TourReport->SetStringField(TEXT("load_error"), TEXT("no launch information (-TwinLaunch or -TwinServer/-TwinProject/-TwinBuild/-TwinToken)"));
			TourSteps.Reset();
			TourSteps.Add([this]() -> float { FinishTour(3); return 10.f; });
			TourNextAt = GetWorld()->GetTimeSeconds();
		}
		if (!TourReport.IsValid())
		{
			return;
		}
	}
	++TourFrames;
	TourSeconds += DeltaTime;
	if (bTourPushForward)
	{
		if (ATwinCharacter* C = TwinPawn())
		{
			C->AddMovementInput(FRotationMatrix(FRotator(0.f, GetControlRotation().Yaw, 0.f)).GetUnitAxis(EAxis::X), 1.f);
		}
	}
	if (TourIndex < TourSteps.Num() && GetWorld()->GetTimeSeconds() >= TourNextAt)
	{
		const float Delay = TourSteps[TourIndex]();
		if (Delay < 0.f)
		{
			TourNextAt = GetWorld()->GetTimeSeconds() - Delay;  // run this step again
		}
		else
		{
			++TourIndex;
			TourNextAt = GetWorld()->GetTimeSeconds() + Delay;
		}
	}
}

void ATwinPlayerController::FinishTour(int32 ExitCode)
{
	TArray<TSharedPtr<FJsonValue>> Shots;
	for (const FString& S : TourShots)
	{
		Shots.Add(MakeShared<FJsonValueString>(S));
	}
	TourReport->SetArrayField(TEXT("screenshots"), Shots);
	TourReport->SetNumberField(TEXT("avg_fps"), TourSeconds > 0 ? TourFrames / TourSeconds : 0.0);
	TourReport->SetNumberField(TEXT("exit_code"), ExitCode);
	FString Text;
	const TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Text);
	FJsonSerializer::Serialize(TourReport.ToSharedRef(), Writer);
	FFileHelper::SaveStringToFile(Text, *(TourOut / TEXT("tour_report.json")), FFileHelper::EEncodingOptions::ForceUTF8WithoutBOM);
	UE_LOG(LogTemp, Display, TEXT("[twin] tour finished: %s"), *(TourOut / TEXT("tour_report.json")));
	if (ExitCode != 0)
	{
		// only a forced exit carries the code on Windows; the report above is already on disk
		FPlatformMisc::RequestExitWithStatus(true, (uint8)ExitCode);
	}
}

void ATwinPlayerController::AddFloorSteps()
{
	// multi-floor buildings: walk up through every floor and back down with PgUp / PgDn, then one cutaway view per
	// floor (ceilings and the floors above hidden)
	ATwinViewerDirector* D0 = Director.Get();
	if (!D0 || !D0->IsMultiFloor())
	{
		return;
	}
	const int32 N = D0->Floors.Num();
	TourSteps.Add([this]() -> float
	{
		TourFloors = MakeShared<FJsonObject>();
		TourJumps.Reset();
		TArray<TSharedPtr<FJsonValue>> List;
		for (const FTwinFloor& F : Director->Floors)
		{
			TSharedPtr<FJsonObject> O = MakeShared<FJsonObject>();
			O->SetStringField(TEXT("key"), F.Key);
			O->SetStringField(TEXT("label"), F.Label);
			O->SetStringField(TEXT("project_name"), F.ProjectName);
			O->SetNumberField(TEXT("floor_z_cm"), F.FloorZ);
			int32 Rooms = 0;
			for (const auto& R : RoomFloors)
			{
				Rooms += (R.IsValid() && TwinMeta::Get(R.Get(), TEXT("Floor")) == F.Key) ? 1 : 0;
			}
			O->SetNumberField(TEXT("rooms"), Rooms);
			List.Add(MakeShared<FJsonValueObject>(O));
		}
		TourFloors->SetNumberField(TEXT("count"), Director->Floors.Num());
		TourFloors->SetArrayField(TEXT("floors"), List);
		Selected.Reset();
		SetCeilingsVisible(true);
		if (ATwinCharacter* C = TwinPawn())
		{
			C->SetGhost(false);
			C->SetFlying(false);
		}
		TeleportToRoom(StartRoom);
		CurrentFloor = FMath::Max(0, FloorOfActor(RoomFloors.IsValidIndex(StartRoom) ? RoomFloors[StartRoom].Get() : nullptr));
		return 1.5f;
	});
	auto Jump = [this](int32 Direction)
	{
		TourSteps.Add([this, Direction]() -> float
		{
			TSharedPtr<FJsonObject> J = MakeShared<FJsonObject>();
			J->SetNumberField(TEXT("from"), CurrentFloor);
			J->SetStringField(TEXT("method"), JumpFloor(Direction));
			J->SetNumberField(TEXT("to"), CurrentFloor);
			TourJumps.Add(MakeShared<FJsonValueObject>(J));
			return 1.5f;  // land, settle on the floor
		});
		TourSteps.Add([this]() -> float
		{
			TSharedPtr<FJsonObject> J = TourJumps.Last()->AsObject();
			UpdateCurrentRoom();
			const ATwinViewerDirector* D = Director.Get();
			const FVector P = GetPawn() ? GetPawn()->GetActorLocation() : FVector::ZeroVector;
			const float Feet = P.Z - ATwinCharacter::CapsuleHalfHeightCm;
			const int32 To = (int32)J->GetNumberField(TEXT("to"));
			const FString Landed = TwinMeta::Get(CurrentRoomFloor.Get(), TEXT("Floor"));
			J->SetArrayField(TEXT("location_cm"), VecJson(P));
			J->SetStringField(TEXT("room"), TwinMeta::Get(CurrentRoomFloor.Get(), TEXT("RoomName")));
			J->SetStringField(TEXT("landed_on_floor"), Landed);
			J->SetNumberField(TEXT("feet_above_floor_cm"), D && D->Floors.IsValidIndex(To) ? Feet - D->Floors[To].FloorZ : 0.0);
			J->SetBoolField(TEXT("ok"), D && D->Floors.IsValidIndex(To) && Landed == D->Floors[To].Key &&
			                FMath::Abs(Feet - D->Floors[To].FloorZ) < 60.f);
			if (D && D->Floors.IsValidIndex(To))
			{
				Shot(FString::Printf(TEXT("floor_%s"), *D->Floors[To].Key));
			}
			return 0.6f;
		});
	};
	for (int32 i = 1; i < N; ++i)
	{
		Jump(1);
	}
	for (int32 i = 1; i < N; ++i)
	{
		Jump(-1);
	}
	for (int32 i = 0; i < N; ++i)
	{
		TourSteps.Add([this, i]() -> float
		{
			ATwinViewerDirector* D = Director.Get();
			if (!D || !D->Floors.IsValidIndex(i))
			{
				return 0.1f;
			}
			if (ATwinCharacter* C = TwinPawn())
			{
				C->SetGhost(true);
			}
			CurrentFloor = i;
			SetCeilingsVisible(false);  // + the floors above: a cutaway of floor i
			FBox B(ForceInit);
			for (const auto& R : RoomFloors)
			{
				if (R.IsValid() && TwinMeta::Get(R.Get(), TEXT("Floor")) == D->Floors[i].Key)
				{
					B += R->GetComponentsBoundingBox(true);
				}
			}
			if (!B.IsValid)
			{
				B = D->Floors[i].Bounds;
			}
			const FVector Center = B.GetCenter();
			const float Span = FMath::Max(B.GetSize().X, B.GetSize().Y);
			PlaceCamera(Center + FVector(-0.45f * Span, 0.75f * Span, 0.95f * Span), Center);
			return 2.5f;
		});
		TourSteps.Add([this, i]() -> float
		{
			Shot(FString::Printf(TEXT("floor_overview_%s"), *Director->Floors[i].Key));
			return 0.8f;
		});
	}
	TourSteps.Add([this]() -> float
	{
		bool bAll = TourJumps.Num() > 0;
		for (const TSharedPtr<FJsonValue>& J : TourJumps)
		{
			bAll &= J->AsObject()->GetBoolField(TEXT("ok"));
		}
		TourFloors->SetArrayField(TEXT("jumps"), TourJumps);
		TourFloors->SetBoolField(TEXT("all_jumps_ok"), bAll);
		TourReport->SetObjectField(TEXT("floors"), TourFloors);
		CurrentFloor = 0;
		SetCeilingsVisible(true);
		return 0.2f;
	});
}
