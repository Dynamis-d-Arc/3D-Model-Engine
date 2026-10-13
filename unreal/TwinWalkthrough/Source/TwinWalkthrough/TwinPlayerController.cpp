#include "TwinPlayerController.h"

#include "Blueprint/AIBlueprintHelperLibrary.h"
#include "Camera/CameraComponent.h"
#include "Dom/JsonObject.h"
#include "Engine/World.h"
#include "EngineUtils.h"
#include "GameFramework/CharacterMovementComponent.h"
#include "HAL/FileManager.h"
#include "Kismet/KismetMathLibrary.h"
#include "Misc/CommandLine.h"
#include "Misc/FileHelper.h"
#include "Misc/Paths.h"
#include "NavigationPath.h"
#include "NavigationSystem.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"
#include "TwinCharacter.h"
#include "TwinMeta.h"
#include "UnrealClient.h"

namespace
{
	TArray<TSharedPtr<FJsonValue>> VecJson(const FVector& V)
	{
		return {MakeShared<FJsonValueNumber>(V.X), MakeShared<FJsonValueNumber>(V.Y), MakeShared<FJsonValueNumber>(V.Z)};
	}
}

void ATwinPlayerController::BeginPlay()
{
	Super::BeginPlay();
	for (TActorIterator<AActor> It(GetWorld()); It; ++It)
	{
		AActor* A = *It;
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
	SetInputMode(FInputModeGameOnly());
	bShowMouseCursor = false;
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
		StartTour();
	}
	else
	{
		Notify(FString::Printf(TEXT("%d rooms loaded. H for help."), RoomFloors.Num()), 5.f);
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
		return TEXT("Ghost (no collision)");
	}
	return C->IsFlying() ? TEXT("Fly") : TEXT("Walk");
}

void ATwinPlayerController::PlayerTick(float DeltaTime)
{
	Super::PlayerTick(DeltaTime);
	if (bTour)
	{
		TickTour(DeltaTime);
	}
	else
	{
		HandleInput(DeltaTime);
	}
	UpdateCurrentRoom();
}

void ATwinPlayerController::HandleInput(float DeltaTime)
{
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

	float MX = 0.f, MY = 0.f;
	GetInputMouseDelta(MX, MY);
	const float KeyYaw = (IsInputKeyDown(EKeys::Right) ? 1.f : 0.f) - (IsInputKeyDown(EKeys::Left) ? 1.f : 0.f);
	const float KeyPitch = (IsInputKeyDown(EKeys::Up) ? 1.f : 0.f) - (IsInputKeyDown(EKeys::Down) ? 1.f : 0.f);
	AddYawInput(MX * 0.25f + KeyYaw * 90.f * DeltaTime);
	AddPitchInput(-MY * 0.25f - KeyPitch * 60.f * DeltaTime);

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
	}
	if (WasInputKeyJustPressed(EKeys::X))
	{
		Selected.Reset();
	}
	if (WasInputKeyJustPressed(EKeys::LeftMouseButton))
	{
		SelectUnderCrosshair();
	}
	if (WasInputKeyJustPressed(EKeys::RightMouseButton))
	{
		WalkToCrosshair();
	}
	if (WasInputKeyJustPressed(EKeys::P))
	{
		ConsoleCommand(TEXT("HighResShot 2"));
		Notify(TEXT("Screenshot saved to Saved/Screenshots"));
	}
	if (WasInputKeyJustPressed(EKeys::Escape))
	{
		ConsoleCommand(TEXT("quit"));
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
	FCollisionQueryParams Params(SCENE_QUERY_STAT(TwinSelect), true, GetPawn());
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
		Notify(TEXT("Click-to-walk works in Walk mode (press F)"));
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
		}
	}
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
	TourNextAt = GetWorld()->GetTimeSeconds() + 3.0;  // let streaming, Lumen and the nav mesh settle

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
		TourReport->SetObjectField(TEXT("by_kind"), ByKind);
		TArray<TSharedPtr<FJsonValue>> Rooms;
		for (const auto& F : RoomFloors)
		{
			TSharedPtr<FJsonObject> R = MakeShared<FJsonObject>();
			R->SetStringField(TEXT("id"), TwinMeta::Get(F.Get(), TEXT("Room")));
			R->SetStringField(TEXT("name"), TwinMeta::Get(F.Get(), TEXT("RoomName")));
			R->SetArrayField(TEXT("spawn_cm"), VecJson(RoomSpawn(F.Get())));
			Rooms.Add(MakeShared<FJsonValueObject>(R));
		}
		TourReport->SetArrayField(TEXT("rooms"), Rooms);
		if (const APawn* P = GetPawn())
		{
			TourReport->SetArrayField(TEXT("player_start_cm"), VecJson(P->GetActorLocation()));
		}
		return 0.5f;
	});

	// 2) navigation: path between the first and last room
	TourSteps.Add([this]() -> float
	{
		TSharedPtr<FJsonObject> Nav = MakeShared<FJsonObject>();
		UNavigationSystemV1* NavSys = FNavigationSystem::GetCurrent<UNavigationSystemV1>(GetWorld());
		Nav->SetBoolField(TEXT("system"), NavSys != nullptr);
		if (NavSys && RoomFloors.Num() >= 2)
		{
			const FVector A = RoomSpawn(RoomFloors[0].Get()) + FVector(0, 0, 50);
			const FVector B = RoomSpawn(RoomFloors.Last().Get()) + FVector(0, 0, 50);
			UNavigationPath* Path = NavSys->FindPathToLocationSynchronously(GetWorld(), A, B);
			const bool bValid = Path && Path->IsValid() && !Path->IsPartial();
			Nav->SetStringField(TEXT("from"), TwinMeta::Get(RoomFloors[0].Get(), TEXT("RoomName")));
			Nav->SetStringField(TEXT("to"), TwinMeta::Get(RoomFloors.Last().Get(), TEXT("RoomName")));
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
			// reachability of every room from the first one (diagnostics)
			TArray<TSharedPtr<FJsonValue>> Reach;
			for (const auto& F : RoomFloors)
			{
				const FVector T = RoomSpawn(F.Get()) + FVector(0, 0, 50);
				UNavigationPath* P = NavSys->FindPathToLocationSynchronously(GetWorld(), A, T);
				TSharedPtr<FJsonObject> R = MakeShared<FJsonObject>();
				R->SetStringField(TEXT("room"), TwinMeta::Get(F.Get(), TEXT("RoomName")));
				R->SetBoolField(TEXT("reachable"), P && P->IsValid() && !P->IsPartial());
				R->SetNumberField(TEXT("length_cm"), P ? P->GetPathLength() : -1.0);
				if (P && P->PathPoints.Num())
				{
					R->SetArrayField(TEXT("end_cm"), VecJson(P->PathPoints.Last()));
				}
				Reach.Add(MakeShared<FJsonValueObject>(R));
			}
			Nav->SetArrayField(TEXT("reachability"), Reach);
		}
		TourReport->SetObjectField(TEXT("navigation"), Nav);
		return 0.1f;
	});

	// 3) click-to-walk: path-follow from the first room to the last room through the doors
	TourSteps.Add([this]() -> float
	{
		if (RoomFloors.Num() < 2)
		{
			return 0.1f;
		}
		TeleportToRoom(0);
		WalkTarget = RoomSpawn(RoomFloors.Last().Get());
		WalkStarted = GetWorld()->GetTimeSeconds();
		UAIBlueprintHelperLibrary::SimpleMoveToLocation(this, WalkTarget);
		return 14.0f;
	});
	TourSteps.Add([this]() -> float
	{
		if (RoomFloors.Num() < 2 || !GetPawn())
		{
			return 0.1f;
		}
		const float Remaining = FVector::Dist2D(GetPawn()->GetActorLocation(), WalkTarget);
		TSharedPtr<FJsonObject> Walk = MakeShared<FJsonObject>();
		Walk->SetStringField(TEXT("to"), TwinMeta::Get(RoomFloors.Last().Get(), TEXT("RoomName")));
		Walk->SetNumberField(TEXT("remaining_cm"), Remaining);
		Walk->SetBoolField(TEXT("arrived"), Remaining < 80.f);
		Walk->SetStringField(TEXT("ended_in_room"), TwinMeta::Get(CurrentRoomFloor.Get(), TEXT("RoomName")));
		TourReport->SetObjectField(TEXT("walk"), Walk);
		StopMovement();
		return 0.2f;
	});

	// 4) collision: walk west into the wall from the first room's centre for 3 s
	TourSteps.Add([this]() -> float
	{
		TeleportToRoom(0);
		SetControlRotation(FRotator(0.f, 180.f, 0.f));
		WallStart = GetPawn() ? GetPawn()->GetActorLocation() : FVector::ZeroVector;
		bTourPushForward = true;
		return 3.0f;
	});
	TourSteps.Add([this]() -> float
	{
		bTourPushForward = false;
		TSharedPtr<FJsonObject> Wall = MakeShared<FJsonObject>();
		const AActor* Floor = RoomFloors.Num() ? RoomFloors[0].Get() : nullptr;
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

	// 5) one eye-level screenshot per room (from a corner, looking across the room)
	for (int32 i = 0; i < RoomFloors.Num(); ++i)
	{
		TourSteps.Add([this, i]() -> float
		{
			const AActor* Floor = RoomFloors[i].Get();
			const FBox B = Floor->GetComponentsBoundingBox(true);
			const FVector Spawn = RoomSpawn(Floor);
			const FVector Corner(B.Min.X + 70.f, B.Max.Y - 70.f, Spawn.Z + ATwinCharacter::EyeHeightCm);
			PlaceCamera(Corner, FVector(B.GetCenter().X, B.GetCenter().Y, Spawn.Z + 100.f));
			return 2.0f;
		});
		TourSteps.Add([this, i]() -> float
		{
			Shot(FString::Printf(TEXT("room_%s"), *TwinMeta::Get(RoomFloors[i].Get(), TEXT("Room"))));
			return 0.4f;
		});
	}

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

	// 7) overview from above with ceilings hidden (ghost mode)
	TourSteps.Add([this]() -> float
	{
		Selected.Reset();
		if (ATwinCharacter* C = TwinPawn())
		{
			C->SetGhost(true);
		}
		SetCeilingsVisible(false);
		FBox All(ForceInit);
		for (const auto& F : RoomFloors)
		{
			All += F->GetComponentsBoundingBox(true);
		}
		const FVector Center = All.GetCenter();
		const float Span = FMath::Max(All.GetSize().X, All.GetSize().Y);
		PlaceCamera(Center + FVector(-0.45f * Span, 0.75f * Span, 0.95f * Span), Center);
		return 2.5f;
	});
	TourSteps.Add([this]() -> float { Shot(TEXT("overview")); return 1.0f; });
	// same view with the navigation mesh drawn (green = walkable)
	TourSteps.Add([this]() -> float { ConsoleCommand(TEXT("show Navigation")); return 1.0f; });
	TourSteps.Add([this]() -> float { Shot(TEXT("navmesh")); return 1.0f; });
	TourSteps.Add([this]() -> float { ConsoleCommand(TEXT("show Navigation")); FinishTour(); return 2.0f; });
	TourSteps.Add([this]() -> float { FGenericPlatformMisc::RequestExit(false); return 10.f; });
}

void ATwinPlayerController::TickTour(float DeltaTime)
{
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
		const float Delay = TourSteps[TourIndex++]();
		TourNextAt = GetWorld()->GetTimeSeconds() + Delay;
	}
}

void ATwinPlayerController::FinishTour()
{
	TArray<TSharedPtr<FJsonValue>> Shots;
	for (const FString& S : TourShots)
	{
		Shots.Add(MakeShared<FJsonValueString>(S));
	}
	TourReport->SetArrayField(TEXT("screenshots"), Shots);
	TourReport->SetNumberField(TEXT("avg_fps"), TourSeconds > 0 ? TourFrames / TourSeconds : 0.0);
	FString Text;
	const TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Text);
	FJsonSerializer::Serialize(TourReport.ToSharedRef(), Writer);
	FFileHelper::SaveStringToFile(Text, *(TourOut / TEXT("tour_report.json")));
	UE_LOG(LogTemp, Display, TEXT("[twin] tour finished: %s"), *(TourOut / TEXT("tour_report.json")));
}
