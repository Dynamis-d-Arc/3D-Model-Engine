#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Actor.h"
#include "Interfaces/IHttpRequest.h"
#include "TwinGltf.h"
#include "TwinViewerDirector.generated.h"

class ATwinAirflowActor;
class FJsonObject;
class UMaterialInstanceDynamic;
class UMaterialInterface;
class UTexture2D;

/** Where to load a building from: backend URL, ids and a short-lived read-only viewer token (never an API key). */
struct FTwinLaunch
{
	FString Server;
	FString ProjectId;
	FString BuildId;
	FString Token;
	FString ProjectName;
	double ExpiresAt = 0.0;

	bool IsComplete() const { return !Server.IsEmpty() && !ProjectId.IsEmpty() && !BuildId.IsEmpty() && !Token.IsEmpty(); }
};

/** One unit's latest reading as served by GET .../hvac/live. Missing values are NaN. */
struct FTwinHvacReading
{
	FString UnitId;
	FString Tag;
	FString Mode;
	FString Alarm;
	bool bOn = false;
	float SetpointC = NAN;
	float SupplyC = NAN;
	float ReturnC = NAN;
	float RoomC = NAN;
	float Fan = NAN;
	float AirflowLps = NAN;
	double Ts = 0.0;
};

struct FTwinTelemetry
{
	FString Source;          // live | simulated | unavailable | "" (not fetched yet)
	FString Label;           // e.g. "Simulated BMS (demonstration data, not measured)"
	bool bMeasured = false;  // true only for real BMS readings
	double DataTs = 0.0;
	double ReceivedAt = 0.0; // FPlatformTime::Seconds() of the last good response
	TMap<FString, FTwinHvacReading> Units;
	int32 BuildUnits = 0;
	int32 Missing = 0;
	int32 Unknown = 0;
	int32 Failures = 0;      // consecutive failed polls
	FString Error;
	float OutdoorC = NAN;
};

/**
 * One floor of the loaded building. A multi-floor building (manifest "floors", twin.viewer.stacks) has one per
 * storey, bottom first, keys F1, F2, ...; a single build is one floor with an empty key.
 */
struct FTwinFloor
{
	FString Key;
	FString Label;            // e.g. "Level 2"
	FString ProjectName;
	float FloorZ = 0.f;       // floor finish, Unreal cm
	FVector Offset = FVector::ZeroVector;
	FBox Bounds = FBox(ForceInit);
	FVector PlayerStart = FVector::ZeroVector;
	float PlayerYaw = 0.f;
	FString PlayerRoom;
};

/** Where a visitor lands on another floor (see ATwinViewerDirector::FloorSpot). */
struct FTwinFloorSpot
{
	FVector Same = FVector::ZeroVector;   // the same plan position, on the floor of the room found there
	bool bSameValid = false;
	FVector Fallback = FVector::ZeroVector; // that room's spawn point, else a stair / lift, else the floor's start
	FString RoomName;
	FString Method;                         // "same spot" | "stair or lift" | "floor entrance"
};

struct FTwinBuildingEntry
{
	FString ProjectId;
	FString Name;
	FString BuildId;
	int32 Version = 0;
	int64 Bytes = 0;
	int32 Rooms = 0;
	int32 Furniture = 0;
};

UENUM()
enum class ETwinLoadState : uint8
{
	Idle,              // no launch information yet
	FetchingManifest,
	Downloading,
	Parsing,
	Building,
	Validating,
	Ready,
	Error,
};

DECLARE_MULTICAST_DELEGATE(FTwinViewerEvent);

/**
 * Runtime building loader of the packaged viewer (no editor needed anywhere):
 *   GET manifest -> GET model.glb (cached by sha256) -> parse on a worker thread -> spawn meshes with
 *   collision in time slices -> place lights / nav bounds -> compare every actor's bounds with the scene
 *   model -> Ready. Then polls HVAC telemetry and fetches the airflow illustration.
 * Spawned actors carry the same "Twin*" tags as editor-imported maps, so the controller/HUD work on both.
 */
UCLASS()
class TWINWALKTHROUGH_API ATwinViewerDirector : public AActor
{
	GENERATED_BODY()

public:
	ATwinViewerDirector();

	static ATwinViewerDirector* Get(const UWorld* World);

	/** Launch info from -TwinLaunch=<file.twinview> or -TwinServer/-TwinProject/-TwinBuild/-TwinToken(-File). */
	static bool LaunchFromCommandLine(FTwinLaunch& Out, FString& Error);
	/** Parse a .twinview launch document (JSON); also accepts it pasted from the clipboard. */
	static bool ParseLaunch(const FString& Text, FTwinLaunch& Out, FString& Error);

	void Open(const FTwinLaunch& InLaunch);
	void OpenBuild(const FString& ProjectId, const FString& BuildId);
	void Retry();
	void RefreshBuildings();
	void SetAirflowVisible(bool bVisible);
	bool IsAirflowVisible() const;
	/** The airflow illustration of one floor (multi-floor buildings get one actor per floor), else the first. */
	ATwinAirflowActor* AirflowForFloor(int32 FloorIndex) const;

	// ---- floors ----------------------------------------------------------------------------------
	bool IsMultiFloor() const { return Floors.Num() > 1; }
	int32 FloorIndexOf(const FString& Key) const;
	/** The floor a point at height Z (Unreal cm, e.g. the visitor's feet) is on. */
	int32 FloorIndexAt(float Z) const;
	/** Landing places on floor FloorIndex for a visitor at plan position XY. */
	FTwinFloorSpot FloorSpot(int32 FloorIndex, const FVector2D& XY) const;

	bool IsReady() const { return State == ETwinLoadState::Ready; }
	bool IsBusy() const { return State != ETwinLoadState::Ready && State != ETwinLoadState::Error && State != ETwinLoadState::Idle; }
	const FTwinHvacReading* ReadingFor(const FString& UnitId) const { return Telemetry.Units.Find(UnitId); }
	TSharedPtr<FJsonObject> ObjectMeta(const FString& Name) const;

	// ---- state read by the HUD / controller / tour ----------------------------------------------
	ETwinLoadState State = ETwinLoadState::Idle;
	FString Stage;
	float Progress = 0.f;
	FString ErrorCode;
	FString ErrorMessage;
	int64 BytesDone = 0;
	int64 BytesTotal = 0;
	bool bFromCache = false;
	FTwinLaunch Launch;
	TSharedPtr<FJsonObject> Manifest;
	FTwinTelemetry Telemetry;
	TArray<FTwinFloor> Floors;            // bottom first; one entry for a single build
	TArray<FTwinBuildingEntry> Buildings;
	FString BuildingsError;
	bool bBuildingsLoading = false;
	TSharedPtr<FJsonObject> Airflow;     // GET .../hvac/airflow (illustration geometry + MATLAB state)
	FString AirflowError;
	TSharedPtr<FJsonObject> Validation;  // written to Saved/TwinViewer/validation_<build>.json
	double MaxDeviationCm = 0.0;
	int32 ValidationChecked = 0;
	int32 ValidationFailures = 0;
	int32 ActorsSpawned = 0;
	int64 TrianglesSpawned = 0;
	TMap<FString, double> Timings;      // seconds per stage
	int32 LoadCount = 0;                 // number of successful loads in this session

	FTwinViewerEvent OnLoaded;           // a building finished loading (actors exist, validated)
	FTwinViewerEvent OnUnloaded;         // the previous building's actors were removed

	UPROPERTY()
	TObjectPtr<ATwinAirflowActor> AirflowActor;          // the first floor's (kept for single builds and callers)
	UPROPERTY()
	TArray<TObjectPtr<ATwinAirflowActor>> AirflowActors; // one per floor with HVAC

	virtual void Tick(float DeltaSeconds) override;

protected:
	virtual void BeginPlay() override;
	virtual void EndPlay(const EEndPlayReason::Type Reason) override;

private:
	using FJsonDone = TFunction<void(TSharedPtr<FJsonObject> Json)>;
	using FFail = TFunction<void(const FString& Code, const FString& Message)>;

	TSharedRef<IHttpRequest, ESPMode::ThreadSafe> NewRequest(const FString& Path, float TimeoutS) const;
	void GetJson(const FString& Path, FJsonDone OnOk, FFail OnFail, float TimeoutS = 30.f);
	static bool ErrorFromResponse(FHttpResponsePtr Response, bool bOk, const FString& Server, FString& Code, FString& Message);
	FString BuildPath(const FString& Leaf) const;

	void Fail(const FString& Code, const FString& Message);
	void Unload();
	void FetchManifest();
	void FetchModel();
	void ParseModel(TArray<uint8>&& Bytes);
	void BeginBuild();
	bool BuildStep(double Budget);
	void SpawnNode(const TwinGltf::FNode& Node);
	void SpawnEnvironment();
	void PlaceNavBounds();
	void ValidateBuild();
	void FinishLoad();
	void PollTelemetry();
	void FetchAirflow();
	void ApplyTelemetry(const TSharedPtr<FJsonObject>& Json);
	FString CachePath() const;
	void ReadFloors();
	UMaterialInterface* MaterialFor(int32 GltfMaterial) const;

	TSharedPtr<TwinGltf::FModel> Model;
	TMap<FString, TSharedPtr<FJsonObject>> MetaByName;
	TMap<FString, TSharedPtr<FJsonObject>> RoomsById;
	int32 BuildCursor = 0;
	int32 BuildPhase = 0;
	double StageStart = 0.0;
	double LoadStart = 0.0;
	double NextPoll = 0.0;
	int32 RequestSerial = 0;             // responses of an older load are ignored

	bool bPollInFlight = false;

	UPROPERTY()
	TArray<TObjectPtr<AActor>> NodeActors;  // one per glTF node, aligned with ExpectedBox / NodeNames
	TArray<FBox> ExpectedBox;                // scene-model bounds (cm) from the viewer manifest
	TArray<FString> NodeNames;

	UPROPERTY()
	TArray<TObjectPtr<AActor>> Spawned;      // lights, ground, project info
	UPROPERTY()
	TArray<TObjectPtr<UTexture2D>> Textures;
	UPROPERTY()
	TArray<TObjectPtr<UMaterialInstanceDynamic>> Materials;
	UPROPERTY()
	TObjectPtr<UMaterialInterface> SurfaceBase;
	UPROPERTY()
	TObjectPtr<UMaterialInterface> GlassBase;
	UPROPERTY()
	TObjectPtr<UMaterialInstanceDynamic> DefaultMaterial;

	class IImageWrapperModule* ImageWrappers = nullptr;
	bool bNoCache = false;
	float RoomLightScale = 0.25f;  // x the build's lumens (-TwinRoomLightScale=)
	int64 MaxModelBytes = 1024ll * 1024 * 1024;
};
